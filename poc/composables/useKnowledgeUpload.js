// Agent knowledge-base uploads (ADR 0046 decision 4). text/plain and
// text/markdown upload the raw file to S3 and let the server chunk it;
// application/pdf is parsed and chunked entirely client-side via the
// vendored pdf.js (poc/vendor/pdfjs/) so the app host never runs a PDF
// parser - only the resulting chunk array is POSTed.
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

  async function uploadKnowledgeFile(file) {
    knowledgeUploadBusy.value = true;
    knowledgeError.value = '';
    try {
      const mimeType = file.type || (file.name.endsWith('.md') ? 'text/markdown' : 'text/plain');
      const isPdf = mimeType === 'application/pdf';

      // PDF text/chunks are computed BEFORE requesting the upload ticket -
      // no point uploading a scanned/image-only PDF that yields no text.
      let chunks = null;
      if (isPdf) {
        chunks = await extractPdfChunks(file);
        if (!chunks.length) {
          throw new Error('no extractable text found - scanned/image-only PDFs are not supported');
        }
      }

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
  };
}
