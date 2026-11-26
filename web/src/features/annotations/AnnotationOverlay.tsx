import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { PdfAnnotation } from "./annotations";


type AnnotationOverlayProps = {
  annotations: PdfAnnotation[];
  pageTransform?: number[];
  activeAnnotationId?: string | null;
  onOpen?(annotationId: string): void;
};


function pdfPointToViewport(
  x: number,
  y: number,
  transform: number[],
): { x: number; y: number } {
  const [a = 1, b = 0, c = 0, d = 1, e = 0, f = 0] = transform;
  return { x: a * x + c * y + e, y: b * x + d * y + f };
}

// Annotations are separate PDF objects. Overlapping marks must not hide notes.
function overlaps(a: number[], b: number[]): boolean {
  const bounds = (q: number[]) => ({
    left: Math.min(q[0]!, q[2]!, q[4]!, q[6]!), right: Math.max(q[0]!, q[2]!, q[4]!, q[6]!),
    top: Math.min(q[1]!, q[3]!, q[5]!, q[7]!), bottom: Math.max(q[1]!, q[3]!, q[5]!, q[7]!),
  });
  if (a.length !== 8 || b.length !== 8) return false;
  const x = bounds(a), y = bounds(b);
  return Math.min(x.right, y.right) > Math.max(x.left, y.left)
    && Math.min(x.bottom, y.bottom) > Math.max(x.top, y.top);
}


export function AnnotationOverlay({
  annotations,
  pageTransform,
  activeAnnotationId,
  onOpen,
}: AnnotationOverlayProps) {
  const [preview, setPreview] = useState<{
    annotationId: string;
    anchor: { left: number; top: number; bottom: number };
  } | null>(null);
  const [position, setPosition] = useState({ left: 12, top: 12 });
  const cardRef = useRef<HTMLElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const openTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const hovered = useRef<string | null>(null);
  const previewId = useId();
  const annotation = annotations.find(item => item.annotation_id === preview?.annotationId);
  const relatedComments = annotation ? annotations.filter(item => item.annotation_id !== annotation.annotation_id
    && item.comment.trim() && item.quads.some(q => annotation.quads.some(other => overlaps(q, other)))) : [];

  const clearTimers = useCallback(() => {
    clearTimeout(openTimer.current);
    clearTimeout(closeTimer.current);
  }, []);
  const close = useCallback(() => {
    clearTimers();
    hovered.current = null;
    setPreview(null);
  }, [clearTimers]);

  useEffect(() => {
    close();
    return clearTimers;
  }, [annotations, pageTransform, close, clearTimers]);

  useEffect(() => {
    const onScroll = (event: Event) => {
      if (!(event.target instanceof Node) || !cardRef.current?.contains(event.target)) close();
    };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    const onPointer = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || !cardRef.current?.contains(event.target)) close();
    };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onPointer, true);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onPointer, true);
    };
  }, [close]);

  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!preview || !card) return;
    const bounds = card.getBoundingClientRect();
    const margin = 12;
    const below = preview.anchor.bottom + 8;
    const top = below + bounds.height <= window.innerHeight - margin
      ? below : preview.anchor.top - bounds.height - 8;
    setPosition({
      left: Math.max(margin, Math.min(preview.anchor.left, window.innerWidth - bounds.width - margin)),
      top: Math.max(margin, Math.min(top, window.innerHeight - bounds.height - margin)),
    });
  }, [preview, annotation]);

  function show(item: PdfAnnotation, element: HTMLElement, immediate = false) {
    clearTimers();
    if (preview?.annotationId === item.annotation_id) return;
    const bounds = element.getBoundingClientRect();
    const next = { annotationId: item.annotation_id, anchor: { left: bounds.left, top: bounds.top, bottom: bounds.bottom } };
    if (immediate) setPreview(next);
    else openTimer.current = setTimeout(() => setPreview(next), 350);
  }

  function leave() {
    clearTimers();
    closeTimer.current = setTimeout(() => setPreview(null), 220);
  }

  // Keep the text layer selectable: track the pointer on the page instead of
  // placing invisible hit targets on top of the PDF text.
  useEffect(() => {
    const overlay = overlayRef.current;
    const page = overlay?.parentElement;
    if (!overlay || !page) return;
    hovered.current = null;
    const move = (event: PointerEvent) => {
      if (event.pointerType === "touch" || event.buttons || (event.target instanceof Element && event.target.closest(".selection-overlay__tools"))) {
        hovered.current = null; close(); return;
      }
      const candidates = Array.from(overlay.querySelectorAll<HTMLButtonElement>("[data-annotation-id]"))
        .filter(button => { const r = button.getBoundingClientRect(); return event.clientX >= r.left && event.clientX <= r.right && event.clientY >= r.top && event.clientY <= r.bottom; })
        .sort((a, b) => Number(b.style.zIndex) - Number(a.style.zIndex));
      const button = candidates[0];
      const id = button?.dataset.annotationId ?? null;
      if (hovered.current === id) return;
      hovered.current = id;
      const item = annotations.find(entry => entry.annotation_id === id);
      if (button && item) show(item, button); else leave();
    };
    const out = () => { hovered.current = null; leave(); };
    page.addEventListener("pointermove", move);
    page.addEventListener("pointerleave", out);
    return () => { page.removeEventListener("pointermove", move); page.removeEventListener("pointerleave", out); };
  }, [annotations, pageTransform, close]);

  if (pageTransform === undefined) return null;
  return (
    <><div className="annotation-overlay" ref={overlayRef}>
      {annotations.flatMap((annotation) => annotation.quads.map((quad, index) => {
        const points = Array.from({ length: 4 }, (_, pointIndex) => pdfPointToViewport(
          quad[pointIndex * 2] ?? 0,
          quad[pointIndex * 2 + 1] ?? 0,
          pageTransform,
        ));
        const left = Math.min(...points.map((point) => point.x));
        const top = Math.min(...points.map((point) => point.y));
        const right = Math.max(...points.map((point) => point.x));
        const bottom = Math.max(...points.map((point) => point.y));
        return (
          <button
            aria-label={`${annotation.read_only ? "只读" : "打开"}批注：${annotation.selected_text || annotation.comment || annotation.type}`}
            className={`annotation-overlay__item${annotation.annotation_id === activeAnnotationId ? " annotation-overlay__item--active" : ""}`}
            data-annotation-id={annotation.annotation_id}
            key={`${annotation.annotation_id}:${index}`}
            aria-haspopup="dialog"
            aria-expanded={preview?.annotationId === annotation.annotation_id}
            aria-controls={preview?.annotationId === annotation.annotation_id ? previewId : undefined}
            onPointerEnter={event => {
              if (event.pointerType !== "touch" && !event.buttons) show(annotation, event.currentTarget);
            }}
            onPointerMove={event => { if (event.buttons) close(); }}
            onPointerLeave={leave}
            onFocus={event => {
              if (event.currentTarget.matches(":focus-visible")) show(annotation, event.currentTarget, true);
            }}
            onBlur={event => {
              if (!(event.relatedTarget instanceof Node) || !cardRef.current?.contains(event.relatedTarget)) leave();
            }}
            onClick={() => { close(); onOpen?.(annotation.annotation_id); }}
            style={{ left, top, width: right - left, height: bottom - top, zIndex: annotation.comment.trim() ? 2 : 1 }}
            type="button"
          />
        );
      }))}
    </div>
    {preview && annotation && createPortal(
      <aside
        aria-label="批注预览"
        className="annotation-preview"
        id={previewId}
        ref={cardRef}
        role="dialog"
        style={position}
        onPointerEnter={clearTimers}
        onPointerLeave={leave}
        onFocusCapture={clearTimers}
        onBlurCapture={event => {
          if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) leave();
        }}
        onPointerDown={event => event.stopPropagation()}
        onPointerMove={event => event.stopPropagation()}
        onPointerUp={event => event.stopPropagation()}
        onClick={event => event.stopPropagation()}
      >
        <header><strong>{annotation.type === "highlight" ? "高亮批注" : annotation.type === "underline" ? "下划线批注" : "评论"}</strong>
          <span>第 {annotation.page} 页{annotation.read_only ? " · 只读" : ""}</span>
          <button aria-label="关闭批注预览" type="button" onClick={close}>×</button>
        </header>
        <div className="annotation-preview__body">
          {annotation.comment.trim()
            ? <p className="annotation-preview__comment">{annotation.comment}</p>
            : <p className="annotation-preview__empty">这处已标注，尚未填写评论。</p>}
          {annotation.selected_text && <details><summary>查看原文</summary><blockquote>{annotation.selected_text}</blockquote></details>}
          {relatedComments.length > 0 && <div className="annotation-preview__related"><strong>同一区域的其他评论</strong>{relatedComments.map(item => <div key={item.annotation_id}><p className="annotation-preview__comment">{item.comment}</p>{onOpen && <button type="button" onClick={() => { close(); onOpen(item.annotation_id); }}>在笔记中查看这条评论</button>}</div>)}</div>}
        </div>
        {onOpen && <footer><button type="button" onClick={() => { close(); onOpen(annotation.annotation_id); }}>在笔记中查看</button></footer>}
      </aside>, overlayRef.current?.closest(".workbench-shell") ?? document.body,
    )}
    </>
  );
}
