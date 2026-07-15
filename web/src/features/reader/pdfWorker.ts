import { GlobalWorkerOptions } from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";


if (/^https?:\/\//i.test(pdfWorkerUrl)) {
  throw new Error("remote_pdf_worker_forbidden");
}

GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
