import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
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
import { AnnotationOverlay } from "../annotations/AnnotationOverlay";
import type { PdfAnnotation } from "../annotations/annotations";
import {
  captureSamePageSelection,
  type SelectedText,
  type SelectionCapture,
} from "./selection";
import {
  beginVisualSelection,
  captureVisualSelection,
  type VisualSelectionGesture,
} from "./visualSelection";


const PDF_RANGE_CHUNK_BYTES = 1024 * 1024;
const PAGE_RENDER_RADIUS = 1;
const FALLBACK_PAGE_SIZE = { width: 612, height: 792 };

type PageSize = typeof FALLBACK_PAGE_SIZE;


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
  requestedPage?: number;
  annotations?: PdfAnnotation[];
  activeAnnotationId?: string | null;
  annotationBusy?: boolean;
  onAnnotateSelection?(
    selected: SelectedText,
    type: "highlight" | "underline" | "comment",
    color: string,
    comment?: string,
  ): void;
  onOpenAnnotation?(annotationId: string): void;
  onSelectionClear?(): void;
  onSelectionChange?(selected: SelectedText): void;
  onTranslateSelection?(selected: SelectedText): void;
};


function renderedPageIndexes(current: number, total: number): number[] {
  const first = Math.max(0, current - PAGE_RENDER_RADIUS);
  const last = Math.min(total - 1, current + PAGE_RENDER_RADIUS);
  return Array.from({ length: last - first + 1 }, (_, offset) => first + offset);
}


function PdfPage({
  document,
  documentId,
  contentId,
  isRendered,
  pageIndex,
  pageSize,
  scale,
  selected,
  annotations,
  activeAnnotationId,
  annotationBusy,
  selectionDragging,
  onAnnotateSelection,
  onCancelSelection,
  onOpenAnnotation,
  onRendered,
  onTranslateSelection,
}: {
  document: PDFDocumentProxy;
  documentId: string;
  contentId: string;
  isRendered: boolean;
  pageIndex: number;
  pageSize: PageSize;
  scale: number;
  selected: SelectedText | null;
  annotations: PdfAnnotation[];
  activeAnnotationId?: string | null;
  annotationBusy?: boolean;
  selectionDragging?: boolean;
  onAnnotateSelection?(
    selected: SelectedText,
    type: "highlight" | "underline" | "comment",
    color: string,
    comment?: string,
  ): void;
  onCancelSelection?(): void;
  onOpenAnnotation?(annotationId: string): void;
  onRendered?(): void;
  onTranslateSelection?(selected: SelectedText): void;
}) {
  const pageRef = useRef<HTMLElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(false);
  const [renderedSize, setRenderedSize] = useState({ width: 0, height: 0 });
  const [viewportTransform, setViewportTransform] = useState<number[] | undefined>();
  const [ocrPage, setOcrPage] = useState<{
    parser_name: "pymupdf" | "mineru";
    coordinate_reliable: boolean;
    blocks: OcrBlock[];
  } | null>(null);

  useEffect(() => {
    if (!isRendered) {
      setOcrPage(null);
      return undefined;
    }
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
  }, [contentId, documentId, isRendered, pageIndex]);

  useEffect(() => {
    if (!isRendered) {
      setError(false);
      return undefined;
    }
    let disposed = false;
    let renderTask: RenderTask | null = null;
    let textLayer: TextLayer | null = null;
    void document.getPage(pageIndex + 1).then(async (page) => {
      if (disposed) return;
      const canvas = canvasRef.current;
      const text = textRef.current;
      if (canvas === null || text === null) return;
      const viewport = page.getViewport({ scale });
      if (pageRef.current !== null) {
        pageRef.current.dataset.pdfTransform = JSON.stringify(viewport.transform);
      }
      setViewportTransform([...viewport.transform]);
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
      if (!disposed) onRendered?.();
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
  }, [document, isRendered, onRendered, pageIndex, scale]);

  const displayedSize = isRendered && renderedSize.width > 0 && renderedSize.height > 0
    ? renderedSize
    : pageSize;

  return (
    <section
      aria-label={`PDF 第 ${pageIndex + 1} 页`}
      className={`pdf-page${isRendered ? "" : " pdf-page--placeholder"}`}
      data-page-number={pageIndex + 1}
      data-rendered-page={isRendered ? "true" : "false"}
      ref={pageRef}
      style={{ height: `${displayedSize.height}px`, width: `${displayedSize.width}px` }}
    >
      {isRendered && (
        <>
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
              busy={annotationBusy}
              onAnnotate={(type, color, comment) => onAnnotateSelection?.(selected, type, color, comment)}
              onCancel={onCancelSelection}
              onTranslate={() => onTranslateSelection?.(selected)}
              scale={scale}
              selected={selected}
              showTools={!selectionDragging}
            />
          )}
          <AnnotationOverlay
            activeAnnotationId={activeAnnotationId}
            annotations={annotations}
            onOpen={onOpenAnnotation}
            pageTransform={viewportTransform}
          />
          {error && <p role="alert">本页暂时无法显示</p>}
        </>
      )}
    </section>
  );
}


export function PdfReader({
  documentId,
  documentVersionId,
  contentId,
  filename,
  initialPage = 1,
  requestedPage,
  annotations = [],
  activeAnnotationId,
  annotationBusy,
  onAnnotateSelection,
  onOpenAnnotation,
  onSelectionClear,
  onSelectionChange,
  onTranslateSelection,
}: PdfReaderProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const previousDocumentIdRef = useRef(documentId);
  const readerPositionRef = useRef({ top: 0, left: 0 });
  const pendingPositionRef = useRef<{ top: number; left: number } | null>(null);
  const restoreFrameRef = useRef<number | null>(null);
  const scrollFrameRef = useRef<number | null>(null);
  const navigationFrameRef = useRef<number | null>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [basePageSizes, setBasePageSizes] = useState<PageSize[]>([]);
  const [pageIndex, setPageIndex] = useState(Math.max(0, initialPage - 1));
  const [scale, setScale] = useState(1);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<SelectedText | null>(null);
  const [selectionHint, setSelectionHint] = useState<string | null>(null);
  const [selectionDragging, setSelectionDragging] = useState(false);
  const visualGestureRef = useRef<{
    gesture: VisualSelectionGesture;
    moved: boolean;
  } | null>(null);
  const selectionGestureRef = useRef<{
    lastValid: { captured: SelectionCapture; range: Range } | null;
  } | null>(null);

  const restoreReaderPosition = useCallback(() => {
    const pending = pendingPositionRef.current;
    if (pending === null || rootRef.current === null) return;
    if (restoreFrameRef.current !== null) cancelAnimationFrame(restoreFrameRef.current);
    restoreFrameRef.current = requestAnimationFrame(() => {
      restoreFrameRef.current = null;
      const root = rootRef.current;
      const next = pendingPositionRef.current;
      if (root === null || next === null) return;
      root.scrollTop = next.top;
      root.scrollLeft = next.left;
      const reached = Math.abs(root.scrollTop - next.top) < 1
        && Math.abs(root.scrollLeft - next.left) < 1;
      if (reached) {
        readerPositionRef.current = next;
        pendingPositionRef.current = null;
      }
    });
  }, []);

  useEffect(() => {
    let disposed = false;
    const root = rootRef.current;
    if (previousDocumentIdRef.current === documentId) {
      const position = root === null
        ? readerPositionRef.current
        : { top: root.scrollTop, left: root.scrollLeft };
      readerPositionRef.current = position;
      pendingPositionRef.current = position;
    } else {
      readerPositionRef.current = { top: 0, left: 0 };
      pendingPositionRef.current = null;
    }
    previousDocumentIdRef.current = documentId;
    setPdf(null);
    setBasePageSizes([]);
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
    let disposed = false;
    if (pdf === null) return undefined;
    void Promise.all(
      Array.from({ length: pdf.numPages }, async (_, index) => {
        try {
          const page = await pdf.getPage(index + 1);
          const viewport = page.getViewport({ scale: 1 });
          return { height: viewport.height, width: viewport.width };
        } catch {
          return FALLBACK_PAGE_SIZE;
        }
      }),
    ).then((sizes) => {
      if (!disposed) setBasePageSizes(sizes);
    });
    return () => {
      disposed = true;
    };
  }, [pdf]);

  useEffect(() => {
    setSelected((current) => {
      if (current === null || current.documentId !== documentId) return null;
      return { ...current, documentVersionId, contentId };
    });
  }, [contentId, documentId, documentVersionId]);

  useEffect(() => {
    setSelected(null);
    setSelectionHint(null);
    setSelectionDragging(false);
    visualGestureRef.current = null;
    selectionGestureRef.current = null;
    onSelectionClear?.();
  }, [documentId, onSelectionClear]);

  useEffect(() => () => {
    if (restoreFrameRef.current !== null) cancelAnimationFrame(restoreFrameRef.current);
    if (scrollFrameRef.current !== null) cancelAnimationFrame(scrollFrameRef.current);
    if (navigationFrameRef.current !== null) cancelAnimationFrame(navigationFrameRef.current);
  }, []);

  const scrollToPage = useCallback((nextIndex: number, behavior: ScrollBehavior) => {
    if (pdf === null) return;
    const targetIndex = Math.min(Math.max(nextIndex, 0), pdf.numPages - 1);
    setPageIndex(targetIndex);
    if (navigationFrameRef.current !== null) cancelAnimationFrame(navigationFrameRef.current);
    navigationFrameRef.current = requestAnimationFrame(() => {
      navigationFrameRef.current = null;
      const root = rootRef.current;
      if (root === null) return;
      const target = root.querySelector<HTMLElement>(`[data-page-number="${targetIndex + 1}"]`);
      if (target === null) return;
      const rootBounds = root.getBoundingClientRect();
      const targetBounds = target.getBoundingClientRect();
      const top = Math.max(0, root.scrollTop + targetBounds.top - rootBounds.top - 16);
      if (typeof root.scrollTo === "function") {
        root.scrollTo({ behavior, left: root.scrollLeft, top });
      } else {
        root.scrollTop = top;
      }
    });
  }, [pdf]);

  useEffect(() => {
    if (requestedPage !== undefined && pdf !== null) {
      scrollToPage(requestedPage - 1, "auto");
    }
  }, [pdf, requestedPage, scrollToPage]);

  const pageIndexes = useMemo(
    () => renderedPageIndexes(pageIndex, pdf?.numPages ?? 0),
    [pageIndex, pdf?.numPages],
  );

  const synchronizePageFromScroll = useCallback((root: HTMLDivElement) => {
    const rootBounds = root.getBoundingClientRect();
    const viewportHeight = root.clientHeight || rootBounds.height;
    if (viewportHeight <= 0) return;
    const viewportCenter = rootBounds.top + viewportHeight / 2;
    let closestIndex = 0;
    let closestDistance = Number.POSITIVE_INFINITY;
    for (const page of root.querySelectorAll<HTMLElement>("[data-page-number]")) {
      const pageNumber = Number(page.dataset.pageNumber);
      const pageBounds = page.getBoundingClientRect();
      const distance = Math.abs(pageBounds.top + pageBounds.height / 2 - viewportCenter);
      if (Number.isInteger(pageNumber) && distance < closestDistance) {
        closestDistance = distance;
        closestIndex = pageNumber - 1;
      }
    }
    setPageIndex((current) => current === closestIndex ? current : closestIndex);
  }, []);

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

  function captureBrowserSelection(allowCrossColumn: boolean): SelectionCapture {
    const root = rootRef.current;
    if (root === null) return { selected: null, hint: null };
    return captureSamePageSelection(
      window.getSelection(),
      root,
      { documentId, documentVersionId, contentId },
      scale,
      { allowCrossColumn },
    );
  }

  function rememberValidSelection(event: ReactPointerEvent<HTMLDivElement>) {
    const visual = visualGestureRef.current;
    if (visual !== null) {
      visual.moved = true;
      if (!event.currentTarget.hasPointerCapture?.(event.pointerId)) {
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }
      const captured = captureVisualSelection(
        visual.gesture,
        event.clientX,
        event.clientY,
        event.altKey,
      );
      window.getSelection()?.removeAllRanges();
      setSelected(captured.selected);
      setSelectionHint(captured.hint);
      event.preventDefault();
      return;
    }
    const gesture = selectionGestureRef.current;
    const browserSelection = window.getSelection();
    if (gesture === null || browserSelection === null || browserSelection.rangeCount !== 1) return;
    const captured = captureBrowserSelection(event.altKey);
    if (captured.selected !== null) {
      gesture.lastValid = {
        captured,
        range: browserSelection.getRangeAt(0).cloneRange(),
      };
      return;
    }
    if (captured.hint !== null && gesture.lastValid !== null) {
      browserSelection.removeAllRanges();
      browserSelection.addRange(gesture.lastValid.range);
    }
  }

  function captureSelection(event: ReactPointerEvent<HTMLDivElement>) {
    if (
      event.target instanceof Element
      && event.target.closest(".selection-overlay__tools") !== null
    ) {
      return;
    }
    const visual = visualGestureRef.current;
    if (visual !== null) {
      const captured = visual.moved
        ? captureVisualSelection(
            visual.gesture,
            event.clientX,
            event.clientY,
            event.altKey,
          )
        : { selected: null, hint: null };
      window.getSelection()?.removeAllRanges();
      visualGestureRef.current = null;
      selectionGestureRef.current = null;
      setSelectionDragging(false);
      setSelected(captured.selected);
      setSelectionHint(captured.hint);
      if (captured.selected !== null) onSelectionChange?.(captured.selected);
      if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
      event.preventDefault();
      return;
    }
    const browserSelection = window.getSelection();
    let captured = captureBrowserSelection(event.altKey);
    if (captured.selected === null && selectionGestureRef.current?.lastValid !== null) {
      captured = selectionGestureRef.current?.lastValid.captured ?? captured;
    }
    browserSelection?.removeAllRanges();
    selectionGestureRef.current = null;
    setSelected(captured.selected);
    setSelectionHint(captured.hint);
    if (captured.selected !== null) onSelectionChange?.(captured.selected);
  }

  function startSelection(event: ReactPointerEvent<HTMLDivElement>) {
    if (
      event.target instanceof Element
      && event.target.closest(".selection-overlay__tools") !== null
    ) {
      return;
    }
    const gesture = beginVisualSelection(
      event.target,
      event.clientX,
      event.clientY,
      { documentId, documentVersionId, contentId },
      scale,
    );
    if (gesture === null) {
      visualGestureRef.current = null;
      selectionGestureRef.current = { lastValid: null };
      return;
    }
    visualGestureRef.current = { gesture, moved: false };
    selectionGestureRef.current = null;
    setSelected(null);
    setSelectionHint(null);
    setSelectionDragging(true);
    onSelectionClear?.();
    window.getSelection()?.removeAllRanges();
  }

  function cancelSelectionGesture(event: ReactPointerEvent<HTMLDivElement>) {
    if (visualGestureRef.current === null) return;
    visualGestureRef.current = null;
    selectionGestureRef.current = null;
    setSelectionDragging(false);
    setSelected(null);
    setSelectionHint(null);
    window.getSelection()?.removeAllRanges();
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  if (error) return <p role="alert">PDF 暂时无法打开</p>;
  if (pdf === null) return <p>正在打开 {filename}</p>;

  return (
    <section aria-label={`阅读 ${filename}`} className="pdf-reader">
      <PdfToolbar
        currentPage={pageIndex + 1}
        onFitWidth={() => void fitWidth()}
        onPageChange={(page) => scrollToPage(page - 1, "smooth")}
        onScaleChange={setScale}
        scale={scale}
        totalPages={pdf.numPages}
      />
      <div
        className="pdf-reader__pages"
        onPointerCancel={cancelSelectionGesture}
        onPointerDown={startSelection}
        onPointerMove={rememberValidSelection}
        onPointerUp={captureSelection}
        onScroll={(event) => {
          const root = event.currentTarget;
          if (pendingPositionRef.current === null) {
            readerPositionRef.current = {
              top: root.scrollTop,
              left: root.scrollLeft,
            };
          }
          if (scrollFrameRef.current === null) {
            scrollFrameRef.current = requestAnimationFrame(() => {
              scrollFrameRef.current = null;
              synchronizePageFromScroll(root);
            });
          }
        }}
        ref={rootRef}
      >
        {Array.from({ length: pdf.numPages }, (_, index) => {
          const baseSize = basePageSizes[index] ?? FALLBACK_PAGE_SIZE;
          return (
            <PdfPage
              contentId={contentId}
              document={pdf}
              documentId={documentId}
              isRendered={pageIndexes.includes(index)}
              key={index}
              activeAnnotationId={activeAnnotationId}
              annotationBusy={annotationBusy}
              annotations={annotations.filter((annotation) => annotation.page === index + 1)}
              onAnnotateSelection={onAnnotateSelection}
              onCancelSelection={() => {
                setSelected(null);
                setSelectionHint(null);
                onSelectionClear?.();
              }}
              onOpenAnnotation={onOpenAnnotation}
              onRendered={restoreReaderPosition}
              onTranslateSelection={onTranslateSelection}
              pageIndex={index}
              pageSize={{ height: baseSize.height * scale, width: baseSize.width * scale }}
              scale={scale}
              selected={selected}
              selectionDragging={selectionDragging}
            />
          );
        })}
      </div>
      {selectionHint !== null && <p role="status">{selectionHint}</p>}
    </section>
  );
}
