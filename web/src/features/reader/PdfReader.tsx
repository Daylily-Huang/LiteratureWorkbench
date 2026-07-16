import { useEffect, useMemo, useRef, useState } from "react";
import {
  getDocument,
  TextLayer,
  type PDFDocumentLoadingTask,
  type PDFDocumentProxy,
  type RenderTask,
} from "pdfjs-dist";
import "pdfjs-dist/web/pdf_viewer.css";

import "./pdfWorker";
import { OcrTextLayer, type OcrBlock } from "./OcrTextLayer";
import { PdfToolbar } from "./PdfToolbar";
import { SelectionOverlay } from "./SelectionOverlay";
import {
  captureSamePageSelection,
  type SelectedText,
} from "./selection";


const PDF_RANGE_CHUNK_BYTES = 1024 * 1024;


function canvasOutputScale(): number {
  const ratio = window.devicePixelRatio;
  if (!Number.isFinite(ratio) || ratio < 1) return 1;
  return Math.min(ratio, 2);
}

export type ReaderDocument = {
  documentId: string;
  documentVersionId: string;
  contentId: string;
  filename: string;
};

type PdfReaderProps = ReaderDocument & {
  initialPage?: number;
  onSelectionChange?(selected: SelectedText): void;
  onTranslateSelection?(selected: SelectedText): void;
};


function visiblePageIndexes(current: number, total: number): number[] {
  const first = Math.max(0, current - 1);
  const last = Math.min(total - 1, current + 1);
  return Array.from({ length: last - first + 1 }, (_, offset) => first + offset);
}


function PdfPage({
  document,
  documentId,
  contentId,
  pageIndex,
  scale,
  selected,
  onTranslateSelection,
}: {
  document: PDFDocumentProxy;
  documentId: string;
  contentId: string;
  pageIndex: number;
  scale: number;
  selected: SelectedText | null;
  onTranslateSelection?(selected: SelectedText): void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(false);
  const [renderedSize, setRenderedSize] = useState({ width: 0, height: 0 });
  const [ocrPage, setOcrPage] = useState<{
    parser_name: "pymupdf" | "mineru";
    coordinate_reliable: boolean;
    blocks: OcrBlock[];
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setOcrPage(null);
    void fetch(`/api/documents/${documentId}/pages/${pageIndex + 1}/text`, {
      credentials: "same-origin",
      signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) return;
      const value = await response.json() as {
        content_id: string;
        parser_name: "pymupdf" | "mineru";
        coordinate_reliable: boolean;
        blocks: OcrBlock[];
      };
      if (value.content_id === contentId && value.parser_name === "mineru") {
        setOcrPage(value);
      }
    }).catch((reason: unknown) => {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) {
        setOcrPage(null);
      }
    });
    return () => controller.abort();
  }, [contentId, documentId, pageIndex]);

  useEffect(() => {
    let disposed = false;
    let renderTask: RenderTask | null = null;
    let textLayer: TextLayer | null = null;
    void document.getPage(pageIndex + 1).then(async (page) => {
      if (disposed) return;
      const canvas = canvasRef.current;
      const text = textRef.current;
      if (canvas === null || text === null) return;
      const viewport = page.getViewport({ scale });
      const outputScale = canvasOutputScale();
      canvas.width = Math.ceil(viewport.width * outputScale);
      canvas.height = Math.ceil(viewport.height * outputScale);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      const context = canvas.getContext("2d");
      if (context === null) throw new Error("pdf_canvas_unavailable");
      text.replaceChildren();
      text.style.width = `${viewport.width}px`;
      text.style.height = `${viewport.height}px`;
      setRenderedSize({ width: viewport.width, height: viewport.height });
      renderTask = page.render({
        canvas,
        canvasContext: context,
        transform: [outputScale, 0, 0, outputScale, 0, 0],
        viewport,
      });
      textLayer = new TextLayer({
        container: text,
        textContentSource: page.streamTextContent(),
        viewport,
      });
      await Promise.all([renderTask.promise, textLayer.render()]);
    }).catch((error: unknown) => {
      if (!disposed && !(error instanceof Error && error.name === "RenderingCancelledException")) {
        setError(true);
      }
    });
    return () => {
      disposed = true;
      renderTask?.cancel();
      textLayer?.cancel();
    };
  }, [document, pageIndex, scale]);

  return (
    <section
      aria-label={`PDF 第 ${pageIndex + 1} 页`}
      className="pdf-page"
      data-page-number={pageIndex + 1}
    >
      <canvas aria-hidden="true" ref={canvasRef} />
      <div className="textLayer" ref={textRef} />
      {ocrPage !== null && renderedSize.width > 0 && renderedSize.height > 0 && (
        <OcrTextLayer
          blocks={ocrPage.blocks}
          coordinateReliable={ocrPage.coordinate_reliable}
          parserName={ocrPage.parser_name}
          renderedHeight={renderedSize.height}
          renderedWidth={renderedSize.width}
        />
      )}
      {selected?.pdfPage === pageIndex + 1 && (
        <SelectionOverlay
          onTranslate={() => onTranslateSelection?.(selected)}
          scale={scale}
          selected={selected}
        />
      )}
      {error && <p role="alert">本页暂时无法显示</p>}
    </section>
  );
}


export function PdfReader({
  documentId,
  documentVersionId,
  contentId,
  filename,
  initialPage = 1,
  onSelectionChange,
  onTranslateSelection,
}: PdfReaderProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pageIndex, setPageIndex] = useState(Math.max(0, initialPage - 1));
  const [scale, setScale] = useState(1);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<SelectedText | null>(null);
  const [selectionHint, setSelectionHint] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    setPdf(null);
    setError(false);
    const loadingTask: PDFDocumentLoadingTask = getDocument({
      url: `/api/documents/${documentId}/file`,
      withCredentials: true,
      rangeChunkSize: PDF_RANGE_CHUNK_BYTES,
      disableRange: false,
      cMapUrl: "/pdfjs/cmaps/",
      cMapPacked: true,
      standardFontDataUrl: "/pdfjs/standard_fonts/",
      wasmUrl: "/pdfjs/wasm/",
      iccUrl: "/pdfjs/iccs/",
    });
    void loadingTask.promise.then((loaded) => {
      if (disposed) return;
      setPdf(loaded);
      setPageIndex((current) => Math.min(current, loaded.numPages - 1));
      setError(false);
    }).catch(() => {
      if (!disposed) setError(true);
    });
    return () => {
      disposed = true;
      void loadingTask.destroy();
    };
  }, [documentId, documentVersionId, contentId]);

  useEffect(() => {
    setSelected(null);
    setSelectionHint(null);
  }, [documentId, documentVersionId, contentId, pageIndex]);

  const pageIndexes = useMemo(
    () => visiblePageIndexes(pageIndex, pdf?.numPages ?? 0),
    [pageIndex, pdf?.numPages],
  );

  async function fitWidth() {
    const root = rootRef.current;
    if (root === null || pdf === null) return;
    try {
      const page = await pdf.getPage(pageIndex + 1);
      const unscaledWidth = page.getViewport({ scale: 1 }).width;
      const style = window.getComputedStyle(root);
      const horizontalPadding = (
        Number.parseFloat(style.paddingLeft)
        + Number.parseFloat(style.paddingRight)
      );
      const availableWidth = root.clientWidth - horizontalPadding;
      if (availableWidth > 0 && unscaledWidth > 0) {
        setScale(Math.max(0.25, Math.min(4, availableWidth / unscaledWidth)));
      }
    } catch {
      // A page render already reports reader errors; fit-width stays a no-op on lookup failure.
    }
  }

  function captureSelection() {
    const root = rootRef.current;
    if (root === null) return;
    const browserSelection = window.getSelection();
    const captured = captureSamePageSelection(
      browserSelection,
      root,
      { documentId, documentVersionId, contentId },
      scale,
    );
    browserSelection?.removeAllRanges();
    setSelected(captured.selected);
    setSelectionHint(captured.hint);
    if (captured.selected !== null) onSelectionChange?.(captured.selected);
  }

  if (error) return <p role="alert">PDF 暂时无法打开</p>;
  if (pdf === null) return <p>正在打开 {filename}</p>;

  return (
    <section aria-label={`阅读 ${filename}`} className="pdf-reader">
      <PdfToolbar
        currentPage={pageIndex + 1}
        onFitWidth={() => void fitWidth()}
        onPageChange={(page) => setPageIndex(page - 1)}
        onScaleChange={setScale}
        scale={scale}
        totalPages={pdf.numPages}
      />
      <div className="pdf-reader__pages" onPointerUp={captureSelection} ref={rootRef}>
        {pageIndexes.map((index) => (
          <PdfPage
            contentId={contentId}
            document={pdf}
            documentId={documentId}
            key={index}
            onTranslateSelection={onTranslateSelection}
            pageIndex={index}
            scale={scale}
            selected={selected}
          />
        ))}
      </div>
      {selectionHint !== null && <p role="status">{selectionHint}</p>}
    </section>
  );
}
