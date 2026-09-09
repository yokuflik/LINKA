// Client-side end-to-end encryption for text messages (ADR 0026).
// Global `useE2E(ctx)` factory (no build step, loaded via <script src>).
//
// - Identity keypair: ECDH P-256, private key non-extractable CryptoKey kept in
//   IndexedDB (DB name scoped by user_id); public key exported as JWK and
//   published via PUT /users/me/public-key.
// - Per message: random AES-256-GCM message key (MK) encrypts the plaintext; a
//   fresh ephemeral ECDH keypair wraps MK once per participant (sender included)
//   via ECDH -> HKDF-SHA256 -> AES-256-GCM.
// - enc_header shape: {v:1, alg:"A256GCM", iv, eph_pub:<JWK>, wraps:{"<uid>":{ek,iv}}}
//
// Needs from ctx: apiFetch, currentUser, log, logError, activeChatId,
// privateChatOtherUserId, groupChatMembers. Read call-time via ctx.
function useE2E(ctx) {
  const { ref } = Vue;

  const subtle = (window.crypto && window.crypto.subtle) || null;
  const e2eAvailable = ref(!!subtle);

  // In-memory state -------------------------------------------------------
  let identityPromise = null;        // Promise<{privateKey, publicJwk}>
  const peerKeyCache = new Map();    // user_id(String) -> Promise<CryptoKey|null> (imported pub key)
  const bundleFetched = new Set();   // chat_id(String) already seeded from key-bundle

  // --- IndexedDB (one tiny store, key "identity") -----------------------
  function openDb(userId) {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open('linka_e2e_' + userId, 1);
      req.onupgradeneeded = () => req.result.createObjectStore('keys');
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  function idbGet(db, key) {
    return new Promise((resolve, reject) => {
      const r = db.transaction('keys', 'readonly').objectStore('keys').get(key);
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
  }
  function idbPut(db, key, val) {
    return new Promise((resolve, reject) => {
      const r = db.transaction('keys', 'readwrite').objectStore('keys').put(val, key);
      r.onsuccess = () => resolve();
      r.onerror = () => reject(r.error);
    });
  }

  // --- base64 <-> ArrayBuffer ------------------------------------------
  function abToB64(buf) {
    const bytes = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }
  function b64ToAb(b64) {
    const s = atob(b64);
    const bytes = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) bytes[i] = s.charCodeAt(i);
    return bytes.buffer;
  }
  const enc = new TextEncoder();
  const dec = new TextDecoder();

  // --- identity keypair ------------------------------------------------
  // Generate on first login / recover from IndexedDB. The private key is a live
  // non-extractable CryptoKey; only the public JWK is ever serialised.
  async function loadIdentity() {
    if (!subtle) throw new Error('no-subtle');
    const userId = ctx.currentUser.value && ctx.currentUser.value.id;
    if (!userId) throw new Error('no-user');
    const db = await openDb(userId);
    let rec = await idbGet(db, 'identity');
    if (!rec || !rec.privateKey || !rec.publicJwk) {
      const pair = await subtle.generateKey(
        { name: 'ECDH', namedCurve: 'P-256' },
        false, // non-extractable private key
        ['deriveKey', 'deriveBits'],
      );
      const publicJwk = await subtle.exportKey('jwk', pair.publicKey);
      rec = { privateKey: pair.privateKey, publicJwk };
      await idbPut(db, 'identity', rec);
    }
    return rec;
  }
  function identity() {
    if (!identityPromise) identityPromise = loadIdentity();
    return identityPromise;
  }

  // Publish our public key. Idempotent server-side (upsert); safe to call on
  // every login. Never throws to the caller - a failed publish just means peers
  // can't encrypt to us yet, and a later login retries.
  async function publishPublicKey() {
    if (!subtle) return;
    try {
      const { publicJwk } = await identity();
      await ctx.apiFetch('/users/me/public-key', {
        method: 'PUT',
        body: JSON.stringify({ public_key: publicJwk, algo: 'ECDH-P256' }),
        noRetry: true,
      });
      ctx.log('E2E public key published');
    } catch (err) {
      ctx.logError('E2E publish failed (non-fatal):', err && err.message);
    }
  }

  // --- peer public keys ----------------------------------------------
  function importPeerJwk(jwk) {
    return subtle.importKey('jwk', jwk, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  }

  function cachePeerJwk(userId, jwk) {
    if (!jwk) return;
    peerKeyCache.set(String(userId), importPeerJwk(jwk).catch(() => null));
  }

  // Seed the whole participant set for a chat in one round-trip. Returns the
  // list of participant user ids (from the bundle itself - authoritative even
  // for a freshly-committed draft chat whose member list isn't loaded yet), or
  // null on failure.
  const bundleIds = new Map(); // chat_id(String) -> [user_id(String), ...]
  async function seedFromKeyBundle(chatId) {
    const key = String(chatId);
    if (bundleFetched.has(key)) return bundleIds.get(key) || null;
    try {
      const bundle = await ctx.apiFetch(`/chats/${chatId}/key-bundle`, { noRetry: true });
      const ids = [];
      for (const row of bundle || []) { cachePeerJwk(row.user_id, row.public_key); ids.push(String(row.user_id)); }
      bundleFetched.add(key);
      bundleIds.set(key, ids);
      return ids;
    } catch (err) {
      ctx.logError('E2E key-bundle fetch failed:', err && err.message);
      return null;
    }
  }

  async function peerPublicKey(userId) {
    const k = String(userId);
    if (!peerKeyCache.has(k)) {
      peerKeyCache.set(k, (async () => {
        try {
          const row = await ctx.apiFetch(`/users/${userId}/public-key`, { noRetry: true });
          return await importPeerJwk(row.public_key);
        } catch (err) {
          return null;
        }
      })());
    }
    return peerKeyCache.get(k);
  }

  // A peer changed their key / a group's membership changed: drop caches so the
  // next send re-fetches. Called from useWsRouter.
  function invalidatePeer(userId) { peerKeyCache.delete(String(userId)); }
  function invalidateChatBundle(chatId) { bundleFetched.delete(String(chatId)); bundleIds.delete(String(chatId)); }

  // Resolve the list of participant user ids for a chat from already-loaded
  // client state (private peer + self, or the group member list + self).
  function participantIdsForChat(chatId) {
    const meId = ctx.currentUser.value && ctx.currentUser.value.id;
    const ids = new Set();
    if (meId != null) ids.add(String(meId));
    const members = ctx.groupChatMembers.value[chatId];
    if (members && members.length) {
      for (const m of members) ids.add(String(m.user.id));
    } else {
      const other = ctx.privateChatOtherUserId.value[chatId];
      if (other != null) ids.add(String(other));
    }
    return [...ids];
  }

  // --- HKDF wrap/unwrap of the message key --------------------------
  async function deriveKek(privateKey, peerPublicKey) {
    const bits = await subtle.deriveBits(
      { name: 'ECDH', public: peerPublicKey }, privateKey, 256,
    );
    const hkdfKey = await subtle.importKey('raw', bits, 'HKDF', false, ['deriveKey']);
    return subtle.deriveKey(
      { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info: enc.encode('linka-e2e-mk-wrap-v1') },
      hkdfKey,
      { name: 'AES-GCM', length: 256 },
      false,
      ['encrypt', 'decrypt'],
    );
  }

  // --- public API ---------------------------------------------------
  // encryptFor(chatId, plaintext) -> { ciphertext, enc } or null if E2E can't
  // run for this chat (missing a participant key) - caller then sends plaintext.
  async function encryptFor(chatId, plaintext) {
    if (!subtle) return null;
    try {
      const bundleParticipants = await seedFromKeyBundle(chatId);
      const participantIds = bundleParticipants && bundleParticipants.length
        ? bundleParticipants
        : participantIdsForChat(chatId);
      if (participantIds.length < 2) return null;

      // Resolve every participant's public key; bail (plaintext) if any missing.
      const pubKeys = {};
      for (const uid of participantIds) {
        const pk = await peerPublicKey(uid);
        if (!pk) { ctx.logError('E2E: no public key for participant', uid, '- sending plaintext'); return null; }
        pubKeys[uid] = pk;
      }

      const mk = await subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
      const msgIv = window.crypto.getRandomValues(new Uint8Array(12));
      const ctBuf = await subtle.encrypt({ name: 'AES-GCM', iv: msgIv }, mk, enc.encode(plaintext));

      const eph = await subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits', 'deriveKey']);
      const ephPubJwk = await subtle.exportKey('jwk', eph.publicKey);
      const rawMk = await subtle.exportKey('raw', mk);

      const wraps = {};
      for (const uid of participantIds) {
        const kek = await deriveKek(eph.privateKey, pubKeys[uid]);
        const wrapIv = window.crypto.getRandomValues(new Uint8Array(12));
        const wrapped = await subtle.encrypt({ name: 'AES-GCM', iv: wrapIv }, kek, rawMk);
        wraps[uid] = { ek: abToB64(wrapped), iv: abToB64(wrapIv) };
      }

      return {
        ciphertext: abToB64(ctBuf),
        enc: {
          v: 1,
          alg: 'A256GCM',
          iv: abToB64(msgIv),
          eph_pub: ephPubJwk,
          wraps,
        },
      };
    } catch (err) {
      ctx.logError('E2E encrypt failed, sending plaintext:', err && err.message);
      return null;
    }
  }

  const DECRYPT_PLACEHOLDER = "🔒 This message can't be shown on this device.";

  // decrypt(message) -> plaintext string, or a polite placeholder if it can't be
  // read (no wrap for us, wrong key, corrupt header). Never throws.
  async function decryptMessage(message) {
    if (!message || !message.is_encrypted) return message ? message.content : null;
    if (!subtle) return DECRYPT_PLACEHOLDER;
    const header = message.enc_header;
    const cipher = message.enc_ct || message.content;
    if (!header || !cipher || !header.wraps) return DECRYPT_PLACEHOLDER;
    try {
      const meId = String(ctx.currentUser.value && ctx.currentUser.value.id);
      const wrap = header.wraps[meId];
      if (!wrap) return DECRYPT_PLACEHOLDER;
      const { privateKey } = await identity();
      const ephPub = await importPeerJwk(header.eph_pub);
      const kek = await deriveKek(privateKey, ephPub);
      const rawMk = await subtle.decrypt(
        { name: 'AES-GCM', iv: new Uint8Array(b64ToAb(wrap.iv)) }, kek, b64ToAb(wrap.ek),
      );
      const mk = await subtle.importKey('raw', rawMk, { name: 'AES-GCM' }, false, ['decrypt']);
      const ptBuf = await subtle.decrypt(
        { name: 'AES-GCM', iv: new Uint8Array(b64ToAb(header.iv)) }, mk, b64ToAb(cipher),
      );
      return dec.decode(ptBuf);
    } catch (err) {
      ctx.logError('E2E decrypt failed:', err && err.message);
      return DECRYPT_PLACEHOLDER;
    }
  }

  // Decrypt in place: replace an encrypted row's `content` with the plaintext
  // (or placeholder) and clear the flag so the rest of the UI treats it as a
  // normal text message. Idempotent. Used on history load + live new_message.
  async function decryptInPlace(message) {
    if (!message || !message.is_encrypted || message._e2eDecrypted) return;
    const text = await decryptMessage(message);
    message.content = text;
    message._e2eCiphertext = true;   // marker: original content was ciphertext
    message._e2eDecrypted = true;
    message.is_encrypted = false;
    delete message.enc_ct;
    delete message.enc_header;
  }

  async function decryptListInPlace(list) {
    if (!list || !list.length) return;
    await Promise.all(list.map((m) => decryptInPlace(m)));
  }

  // Logout: drop in-memory state. The identity keypair stays in IndexedDB
  // (scoped by user_id) so the same user logging back in on this device keeps
  // reading their history.
  function resetE2E() {
    identityPromise = null;
    peerKeyCache.clear();
    bundleFetched.clear();
    bundleIds.clear();
  }

  return {
    e2eAvailable,
    initE2E: async () => { if (subtle) { await identity(); await publishPublicKey(); } },
    resetE2E,
    publishPublicKey,
    encryptFor,
    decryptMessage,
    decryptInPlace,
    decryptListInPlace,
    cachePeerJwk,
    invalidatePeer,
    invalidateChatBundle,
    E2E_DECRYPT_PLACEHOLDER: DECRYPT_PLACEHOLDER,
  };
}
