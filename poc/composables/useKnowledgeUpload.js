// Agent knowledge-base uploads (ADR 0046 decision 4). text/plain and
// text/markdown upload the raw file to S3 and let the server chunk it;
// application/pdf is parsed and chunked entirely client-side via the
// vendored pdf.js (poc/vendor/pdfjs/) so the app host never runs a PDF
// parser - only the resulting chunk array is POSTed. A successful commit and
// a PDF-with-no-extractable-text failure are both reported to the owner via
// their agent's own chat, not a toast (ADR 0085) - the failure path calls
// /agents/me/knowledge/report-failure instead of setting knowledgeError.
//
// Global `useKnowledgeUpload(ctx)` factory (no build step, <script src>).
// Needs from ctx: apiFetch, friendlyError, showToast, logError.
function useKnowledgeUpload(ctx) {
  const { ref } = Vue;

  // Mirrors modules/agents/chunking.py's chunk_text - kept in lockstep so
  // retrieval quality doesn't depend on which path (server text/markdown vs
  // client-side PDF) a document took.
  const CHUNK_MAX_CHARS = 1500;
  const CHUNK_OVERLAP_CHARS = 200;

  const knowledgeDocuments = ref([]);
  const knowledgeLoading = ref(false);
  const knowledgeLoaded = ref(false); // true once the first fetch completes (even if empty)
  const knowledgeUploadBusy = ref(false);
  const knowledgeError = ref('');

  function chunkText(text) {
    const stripped = (text || '').trim();
    if (!stripped) return [];
    const chunks = [];
    const step = CHUNK_MAX_CHARS - CHUNK_OVERLAP_CHARS;
    let start = 0;
    while (start < stripped.length) {
      const end = Math.min(start + CHUNK_MAX_CHARS, stripped.length);
      const chunk = stripped.slice(start, end).trim();
      if (chunk) chunks.push(chunk);
      if (end === stripped.length) break;
      start += step;
    }
    return chunks;
  }

  async function extractPdfChunks(file) {
    if (!window.pdfjsLib) {
      throw new Error('PDF support is not available - serve the PoC over http://localhost, not file://');
    }
    const buffer = await file.arrayBuffer();
    const pdf = await window.pdfjsLib.getDocument({ data: buffer }).promise;
    let fullText = '';
    for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
      const page = await pdf.getPage(pageNum);
      const content = await page.getTextContent();
      fullText += content.items.map((item) => item.str).join(' ') + '\n\n';
    }
    return chunkText(fullText);
  }

  async function loadKnowledgeDocuments() {
    knowledgeLoading.value = true;
    try {
      knowledgeDocuments.value = await ctx.apiFetch('/agents/me/knowledge');
    } catch (err) {
      ctx.logError && ctx.logError('failed to load knowledge documents', err);
    } finally {
      knowledgeLoading.value = false;
      knowledgeLoaded.value = true;
    }
  }

  // ADR 0087 step 1 (stage-time): resolve mime type + PDF chunks up front so
  // a scanned/no-text PDF can be rejected before the file is even staged -
  // same "no extractable text" outcome the old instant-upload path had, just
  // surfaced at pick-time instead of at commit-time. Returns null (and
  // reports the failure) when the file should not be staged at all; never
  // throws for that case.
  async function prepareKnowledgeUpload(file) {
    const mimeType = file.type || (file.name.endsWith('.md') ? 'text/markdown' : 'text/plain');
    const isPdf = mimeType === 'application/pdf';
    let chunks = null;
    if (isPdf) {
      chunks = await extractPdfChunks(file);
      if (!chunks.length) {
        // ADR 0085: reported to the owner via the agent's own chat, not a
        // toast - report-failure is best-effort (swallow its own errors) so
        // a flaky report call doesn't mask the real, user-facing point: this
        // file could not be added.
        try {
          await ctx.apiFetch('/agents/me/knowledge/report-failure', {
            method: 'POST',
            body: JSON.stringify({ filename: file.name, mime_type: mimeType, reason: 'no_extractable_text' }),
          });
        } catch (reportErr) {
          ctx.logError && ctx.logError('failed to report knowledge ingestion failure', reportErr);
        }
        return null;
      }
    }
    return { mimeType, chunks };
  }

  // ADR 0087 step 2 (send-time): upload-ticket + PUT + commit, given the
  // mime/chunks `prepareKnowledgeUpload` already resolved. Separate S3
  // bucket/key from any chat-message upload of the same bytes (the
  // `agent_knowledge` bucket has its own MIME allowlist + server-side
  // text/markdown chunking that hardcodes that bucket) - not reusable across
  // the two pipelines, so this always PUTs its own copy.
  async function commitKnowledgeUpload(file, mimeType, chunks) {
    const ticket = await ctx.apiFetch('/agents/me/knowledge/upload-ticket', {
      method: 'POST',
      body: JSON.stringify({ mime_type: mimeType, size_bytes: file.size }),
    });

    const putResp = await fetch(ticket.upload_url, {
      method: 'PUT',
      headers: ticket.required_headers,
      body: file,
    });
    if (!putResp.ok) throw new Error('upload failed (' + putResp.status + ')');

    const document = await ctx.apiFetch('/agents/me/knowledge', {
      method: 'POST',
      body: JSON.stringify({
        filename: file.name,
        storage_key: ticket.storage_key,
        mime_type: mimeType,
        chunks,
      }),
    });
    knowledgeDocuments.value = [document, ...knowledgeDocuments.value];
    return document;
  }

  // Direct "Knowledge base" section entry point (AgentSettingsView.js) -
  // unchanged instant-upload behavior, now composed from the two steps above.
  async function uploadKnowledgeFile(file) {
    knowledgeUploadBusy.value = true;
    knowledgeError.value = '';
    try {
      const prepared = await prepareKnowledgeUpload(file);
      if (!prepared) return;
      await commitKnowledgeUpload(file, prepared.mimeType, prepared.chunks);
      ctx.showToast('Document added to knowledge base');
    } catch (err) {
      knowledgeError.value = ctx.friendlyError(err, "We couldn't upload that document. Please try again.");
    } finally {
      knowledgeUploadBusy.value = false;
    }
  }

  async function deleteKnowledgeDocument(documentId) {
    knowledgeError.value = '';
    try {
      await ctx.apiFetch(`/agents/me/knowledge/${documentId}`, { method: 'DELETE' });
      knowledgeDocuments.value = knowledgeDocuments.value.filter((d) => d.id !== documentId);
    } catch (err) {
      knowledgeError.value = ctx.friendlyError(err, "We couldn't remove that document. Please try again.");
    }
  }

  return {
    knowledgeDocuments, knowledgeLoading, knowledgeLoaded, knowledgeUploadBusy, knowledgeError,
    loadKnowledgeDocuments, uploadKnowledgeFile, deleteKnowledgeDocument,
    prepareKnowledgeUpload, commitKnowledgeUpload,
  };
}
