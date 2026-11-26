import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { SelectedText } from "./selection";
import { cleanPdfCopyText } from "./copyText";


type SelectionOverlayProps = {
  selected: SelectedText;
  scale: number;
  busy?: boolean;
  showTools?: boolean;
  onAnnotate?(type: "highlight" | "underline" | "comment", color: string, comment?: string): void;
  onOpenMarkup?(): void;
  onCancel?(): void;
  onTranslate?(): void;
  onAsk?(): void;
};

const HIGHLIGHT_COLORS = ["yellow", "green", "blue", "pink"];
const UNDERLINE_COLORS = ["red", "blue", "black"];


function remembered(key: string, fallback: string): string {
  try { return window.localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}


export function SelectionOverlay({
  selected,
  scale,
  busy = false,
  showTools = true,
  onAnnotate,
  onOpenMarkup,
  onCancel,
  onTranslate,
  onAsk,
}: SelectionOverlayProps) {
  const [palette, setPalette] = useState<"highlight" | "underline" | null>(null);
  const [commenting, setCommenting] = useState(false);
  const [comment, setComment] = useState("");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => { setCopyState("idle"); }, [selected.documentId, selected.pdfPage, selected.text]);
  const tools = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: selected.buttonAnchor.x * scale, top: selected.buttonAnchor.y * scale + 6 });

  useLayoutEffect(() => {
    const node = tools.current;
    const clip = node?.closest(".pdf-reader__pages");
    if (!node || !clip) return;
    const place = () => {
      const base = node.parentElement?.getBoundingClientRect();
      const bounds = clip.getBoundingClientRect();
      if (!base || !bounds.width || !bounds.height) return;
      // Use inner client area (excludes Windows scrollbars) and actual rendered box size
      const clipLeft = bounds.left + clip.clientLeft;
      const clipTop = bounds.top + clip.clientTop;
      const clipRight = clipLeft + (clip.clientWidth || bounds.width);
      const clipBottom = clipTop + (clip.clientHeight || bounds.height);
      const rect = node.getBoundingClientRect();
      const width = Math.ceil(rect.width || node.offsetWidth);
      const height = Math.ceil(rect.height || node.offsetHeight);
      const margin = 10;
      const minX = clipLeft + margin - base.left;
      const maxX = clipRight - base.left - width - margin;
      const minY = clipTop + margin - base.top;
      const maxY = clipBottom - base.top - height - margin;
      const preferredLeft = selected.buttonAnchor.x * scale;
      const topSelectionY = selected.rects.length > 0
        ? Math.min(...selected.rects.map(r => r.y)) * scale
        : selected.buttonAnchor.y * scale - 20;
      const aboveTop = topSelectionY - height - 8;
      const belowTop = selected.buttonAnchor.y * scale + 6;
      const preferredTop = aboveTop >= minY ? aboveTop : belowTop;
      const left = maxX >= minX
        ? Math.max(minX, Math.min(preferredLeft, maxX))
        : minX;
      const top = maxY >= minY
        ? Math.max(minY, Math.min(preferredTop, maxY))
        : minY;
      setPosition(current => current.left === left && current.top === top ? current : { left, top });
    };
    place();
    window.addEventListener("resize", place);
    clip.addEventListener("scroll", place, { passive: true });
    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(place);
      observer.observe(node);
      observer.observe(clip);
    }
    return () => {
      window.removeEventListener("resize", place);
      clip.removeEventListener("scroll", place);
      observer?.disconnect();
    };
  }, [selected, scale, palette, commenting, showTools, copyState]);

  function annotate(type: "highlight" | "underline", color: string) {
    try { window.localStorage.setItem(`lw-${type}-color`, color); } catch { /* Annotation does not depend on browser storage. */ }
    onAnnotate?.(type, color);
  }

  return (
    <div className="selection-overlay">
      {selected.rects.map((rect, index) => (
        <span
          className="selection-overlay__rect"
          data-testid="selection-rect"
          key={`${rect.x}:${rect.y}:${index}`}
          style={{
            left: rect.x * scale,
            top: rect.y * scale,
            width: rect.width * scale,
            height: rect.height * scale,
          }}
        />
      ))}
      {showTools && <div
        ref={tools}
        className="selection-overlay__tools"
        onPointerDown={(event) => { if (!(event.target instanceof HTMLTextAreaElement)) event.preventDefault(); }}
        style={position}
      >
        <div className="selection-overlay__toolbar">
          <button
            aria-label="翻译所选文字"
            className="selection-overlay__button selection-overlay__button--primary"
            disabled={busy || !selected.translationEligible}
            onClick={onTranslate}
            title={selected.translationEligible ? "结合论文上下文进行学术翻译" : "翻译最多支持 8,000 字，批注不受影响"}
            type="button"
          >
            <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="m5 8 6 6" /><path d="m4 14 6-6 2-3" /><path d="M2 5h12" /><path d="M7 2h1" /><path d="m22 22-5-10-5 10" /><path d="M14 18h6" />
            </svg>
            <span>翻译</span>
          </button>
          {onAsk && (
            <button
              aria-label="向 AI 提问所选文字"
              className="selection-overlay__button"
              disabled={busy || !selected.translationEligible}
              onClick={onAsk}
              title="将选中原文送入 AI 研究助手精读"
              type="button"
            >
              <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3L12 3Z" />
              </svg>
              <span>问 AI</span>
            </button>
          )}
          <button
            aria-label="复制所选文字"
            title="自动整理换行与断词；按住 Shift 点击可复制排版原文"
            className="selection-overlay__button"
            type="button"
            onClick={async event => {
              try {
                await navigator.clipboard.writeText(event.shiftKey
                  ? selected.rawText ?? selected.text
                  : selected.copyText ?? cleanPdfCopyText(selected.text));
                setCopyState("copied");
              } catch {
                setCopyState("failed");
              }
            }}
          >
            <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </svg>
            <span>{copyState === "copied" ? "已复制" : "复制"}</span>
          </button>
          {!selected.translationEligible && <span className="selection-overlay__limit">超过 8,000 字</span>}
          {onAnnotate && (
            <>
              <span aria-hidden="true" className="selection-overlay__divider" />
              <button
                aria-label="高亮"
                className={`selection-overlay__button${palette === "highlight" ? " selection-overlay__button--active" : ""}`}
                disabled={busy}
                onClick={() => {
                  const next = palette === "highlight" ? null : "highlight";
                  setPalette(next);
                  setCommenting(false);
                  if (next !== null) onOpenMarkup?.();
                }}
                title="高亮并直接写入本地 PDF"
                type="button"
              >
                <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#f7cb4d" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="m9 11-6 6v3h9l3-3" /><path d="m22 12-4.6 4.6a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L14 4" />
                </svg>
                <span>高亮</span>
              </button>
              <button
                aria-label="下划线"
                className={`selection-overlay__button${palette === "underline" ? " selection-overlay__button--active" : ""}`}
                disabled={busy}
                onClick={() => {
                  const next = palette === "underline" ? null : "underline";
                  setPalette(next);
                  setCommenting(false);
                  if (next !== null) onOpenMarkup?.();
                }}
                title="下划线并直接写入本地 PDF"
                type="button"
              >
                <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M6 4v6a6 6 0 0 0 12 0V4" /><line x1="4" y1="20" x2="20" y2="20" stroke="#f06b61" strokeWidth="2.4" />
                </svg>
                <span>下划线</span>
              </button>
              <button
                aria-label="评论"
                className={`selection-overlay__button${commenting ? " selection-overlay__button--active" : ""}`}
                disabled={busy}
                onClick={() => {
                  const next = !commenting;
                  setCommenting(next);
                  setPalette(null);
                  if (next) onOpenMarkup?.();
                }}
                title="添加学术评论并写入本地 PDF"
                type="button"
              >
                <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                </svg>
                <span>评论</span>
              </button>
            </>
          )}
          <span aria-hidden="true" className="selection-overlay__divider" />
          <button
            aria-label="取消"
            className="selection-overlay__button selection-overlay__button--close"
            disabled={busy}
            onClick={onCancel}
            title="取消选区"
            type="button"
          >
            <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        {copyState === "failed" && <span role="alert" className="selection-overlay__alert">复制未成功，请使用 Ctrl+C。</span>}
        {palette !== null && (
          <div aria-label={`${palette === "highlight" ? "高亮" : "下划线"}颜色`} className="selection-overlay__palette">
            {(palette === "highlight" ? HIGHLIGHT_COLORS : UNDERLINE_COLORS).map((color) => (
              <button
                disabled={busy}
                aria-label={`${color} ${palette === "highlight" ? "高亮" : "下划线"}`}
                className={`selection-overlay__color selection-overlay__color--${color}`}
                key={color}
                onClick={() => annotate(palette, color)}
                type="button"
              />
            ))}
            <button
              className="selection-overlay__button"
              onClick={() => annotate(
                palette,
                remembered(`lw-${palette}-color`, palette === "highlight" ? "yellow" : "red"),
              )}
              type="button"
            >上次颜色</button>
          </div>
        )}
        {commenting && (
          <div className="selection-overlay__comment-editor">
            <textarea
              aria-label="评论内容"
              autoFocus
              onChange={(event) => setComment(event.target.value)}
              placeholder="写下学术评论（将直接保存到本地 PDF）…"
              value={comment}
            />
            <button
              className="selection-overlay__button selection-overlay__button--save"
              disabled={busy || comment.trim() === ""}
              onClick={() => onAnnotate?.("comment", "blue", comment.trim())}
              type="button"
            >保存评论</button>
          </div>
        )}
      </div>}
    </div>
  );
}
