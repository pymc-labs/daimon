// Loads the vendored pdf.js and announces it to app.js. External rather than
// inline so the page's Content-Security-Policy needs no 'unsafe-inline' for scripts.
import * as pdfjsLib from "/viewer/vendor/pdf.min.mjs";
pdfjsLib.GlobalWorkerOptions.workerSrc = "/viewer/vendor/pdf.worker.min.mjs";
window.pdfjsLib = pdfjsLib;
window.dispatchEvent(new Event("pdfjs-ready"));
