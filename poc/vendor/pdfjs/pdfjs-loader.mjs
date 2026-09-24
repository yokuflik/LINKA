// Vendored pdf.js (ADR 0046 decision 4) - no CDN, same convention as
// poc/vendor/thumbhash.js. pdf.js 4.x only ships an ES module build, so this
// tiny loader imports it and republishes it on window for the rest of the
// PoC's plain <script> files (useKnowledgeUpload.js) to consume.
import * as pdfjsLib from './pdf.min.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = 'vendor/pdfjs/pdf.worker.min.mjs';
window.pdfjsLib = pdfjsLib;
