// Lightweight audio-waveform helpers built purely on the native Web Audio API.
// Two independent concerns:
//   1. liveMeter(stream)      - an AnalyserNode fed by the mic MediaStream while
//                               recording, exposing a small reactive array of
//                               bar heights (0..1) that a component animates.
//   2. peaksForUrl(url)       - decode a finished audio file once and reduce it
//                               to a fixed-length array of peak amplitudes
//                               (0..1) for the static playback waveform. Cached
//                               per URL so re-rendering a bubble is free.
// No CDN library, no build step. Global `useAudioWaveform(ctx)` factory.
function useAudioWaveform(ctx) {
  const { ref } = Vue;

  // One shared AudioContext for the whole PoC - browsers cap how many you may
  // create. Created lazily on first use (needs a user gesture on some browsers).
  let sharedCtx = null;
  function audioContext() {
    if (!sharedCtx) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      sharedCtx = Ctor ? new Ctor() : null;
    }
    if (sharedCtx && sharedCtx.state === 'suspended') sharedCtx.resume().catch(() => {});
    return sharedCtx;
  }

  // ---------------------------------------------------------------
  // 1. Live recording meter
  // ---------------------------------------------------------------
  const LIVE_BARS = 40; // how many bars the live waveform shows

  // Returns { bars, stop }. `bars` is a reactive ref holding an array of
  // LIVE_BARS numbers in 0..1; `stop` tears down the analyser + RAF loop.
  function liveMeter(stream) {
    const bars = ref(new Array(LIVE_BARS).fill(0));
    const ac = audioContext();
    if (!ac || !stream) {
      return { bars, stop() {} };
    }

    // iOS Safari starts the shared AudioContext 'suspended' and won't feed the
    // analyser until it's actually running - resume() is async, so kick it and
    // let the RAF loop pick up data once it lands.
    if (ac.state === 'suspended') ac.resume().catch(() => {});

    const source = ac.createMediaStreamSource(stream);
    const analyser = ac.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.6;
    source.connect(analyser);

    // iOS bug: a MediaStreamSource that isn't connected to the destination
    // produces all-silent (128) time-domain samples. Route it through a muted
    // gain node so the graph runs without any audible mic monitoring.
    let sink = null;
    try {
      sink = ac.createGain();
      sink.gain.value = 0;
      analyser.connect(sink);
      sink.connect(ac.destination);
    } catch (e) { /* non-fatal */ }

    const buffer = new Uint8Array(analyser.frequencyBinCount);
    let raf = 0;
    let stopped = false;

    function tick() {
      if (stopped) return;
      analyser.getByteTimeDomainData(buffer);
      // RMS of the current frame -> one new sample, scrolled in from the right
      // so the waveform visibly moves like WhatsApp's.
      let sum = 0;
      for (let i = 0; i < buffer.length; i++) {
        const v = (buffer[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buffer.length);
      const level = Math.min(1, rms * 3.2); // gain so normal speech fills the bar
      const next = bars.value.slice(1);
      next.push(level);
      bars.value = next;
      raf = requestAnimationFrame(tick);
    }
    raf = requestAnimationFrame(tick);

    return {
      bars,
      stop() {
        stopped = true;
        if (raf) cancelAnimationFrame(raf);
        try { source.disconnect(); } catch (e) { /* already gone */ }
        try { analyser.disconnect(); } catch (e) { /* already gone */ }
        try { if (sink) sink.disconnect(); } catch (e) { /* already gone */ }
      },
    };
  }

  // ---------------------------------------------------------------
  // 2. Static playback peaks
  // ---------------------------------------------------------------
  const PLAYBACK_BARS = 48;
  const peaksCache = new Map(); // url -> Promise<number[]>

  function reducePeaks(channelData, buckets) {
    const blockSize = Math.floor(channelData.length / buckets) || 1;
    const peaks = new Array(buckets).fill(0);
    for (let b = 0; b < buckets; b++) {
      let max = 0;
      const start = b * blockSize;
      for (let i = 0; i < blockSize; i++) {
        const v = Math.abs(channelData[start + i] || 0);
        if (v > max) max = v;
      }
      peaks[b] = max;
    }
    // Normalise so the loudest peak reaches full height.
    const ceiling = Math.max(...peaks) || 1;
    return peaks.map((p) => Math.max(0.04, p / ceiling));
  }

  // Returns a Promise<number[] of PLAYBACK_BARS>. On any failure (CORS, decode,
  // no Web Audio, timeout) resolves to a flat mid-height fallback so the UI
  // still draws - the waveform is decorative, playback works without it.
  // Failures are cached (the rejected fetch stays in peaksCache) so a
  // re-rendered bubble never refetches, and the fetch is bounded by
  // WAVEFORM_FETCH_TIMEOUT_MS so a bucket with no CORS headers can't leave N
  // requests hanging and stalling the chat while a chat's messages load.
  const WAVEFORM_FETCH_TIMEOUT_MS = 4000;

  const FLAT_PEAKS = () => new Array(PLAYBACK_BARS).fill(0.3);

  async function decodeOnce(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), WAVEFORM_FETCH_TIMEOUT_MS);
    try {
      const ac = audioContext();
      if (!ac) throw new Error('no AudioContext');
      // iOS starts the shared context suspended; decodeAudioData on a
      // freshly-recorded blob quietly yields nothing until it's running.
      if (ac.state === 'suspended') { try { await ac.resume(); } catch (e) { /* */ } }
      const resp = await fetch(url, { signal: controller.signal });
      const arrayBuf = await resp.arrayBuffer();
      // Safari's callback-style decodeAudioData is the only reliable form on iOS.
      const audioBuf = await new Promise((resolve, reject) => {
        const p = ac.decodeAudioData(arrayBuf, resolve, reject);
        if (p && p.then) p.then(resolve, reject);
      });
      return reducePeaks(audioBuf.getChannelData(0), PLAYBACK_BARS);
    } finally {
      clearTimeout(timer);
    }
  }

  function peaksForUrl(url) {
    if (!url) return Promise.resolve(FLAT_PEAKS());
    if (peaksCache.has(url)) return peaksCache.get(url);

    const promise = (async () => {
      // One immediate try, then one retry ~350ms later: on iOS a blob recorded
      // milliseconds ago often isn't decodable yet right after send.
      try {
        return await decodeOnce(url);
      } catch (err1) {
        await new Promise((r) => setTimeout(r, 350));
        try {
          return await decodeOnce(url);
        } catch (err2) {
          if (ctx && ctx.log) ctx.log('waveform decode skipped:', err2 && err2.message);
          // Do NOT cache the failure - drop it so a later render (or the real
          // presigned URL once the send reconciles) can decode successfully.
          peaksCache.delete(url);
          return FLAT_PEAKS();
        }
      }
    })();

    peaksCache.set(url, promise);
    return promise;
  }

  // Also expose the playback helper on a global singleton so the globally
  // registered <VoiceMessage> component (which has no ctx) can decode peaks
  // without threading a prop through MessageList.
  const api = { liveMeter, peaksForUrl, LIVE_BARS, PLAYBACK_BARS };
  window.__linkaWaveform = api;
  return api;
}
