import { useState } from "react";

import type { SelectedText } from "./selection";


type SelectionOverlayProps = {
  selected: SelectedText;
  scale: number;
  busy?: boolean;
  showTools?: boolean;
  onAnnotate?(type: "highlight" | "underline" | "comment", color: string, comment?: string): void;
  onCancel?(): void;
  onTranslate?(): void;
};

const HIGHLIGHT_COLORS = ["yellow", "green", "blue", "pink"];
const UNDERLINE_COLORS = ["red", "blue", "black"];


function remembered(key: string, fallback: string): string {
  return window.localStorage.getItem(key) ?? fallback;
}


export function SelectionOverlay({
  selected,
  scale,
  busy = false,
  showTools = true,
  onAnnotate,
  onCancel,
  onTranslate,
}: SelectionOverlayProps) {
  const [palette, setPalette] = useState<"highlight" | "underline" | null>(null);
  const [commenting, setCommenting] = useState(false);
  const [comment, setComment] = useState("");

  function annotate(type: "highlight" | "underline", color: string) {
    window.localStorage.setItem(`lw-${type}-color`, color);
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
        className="selection-overlay__tools"
        onPointerDown={(event) => event.preventDefault()}
        style={{
          left: selected.buttonAnchor.x * scale,
          top: selected.buttonAnchor.y * scale,
        }}
      >
        <div className="selection-overlay__toolbar">
          <button
            aria-label="翻译所选文字"
            className="selection-overlay__button"
            disabled={busy || !selected.translationEligible}
            onClick={onTranslate}
            title={selected.translationEligible ? undefined : "翻译最多支持 8,000 字，批注不受影响"}
            type="button"
          >翻译</button>
          {!selected.translationEligible && <span className="selection-overlay__limit">超过 8,000 字</span>}
          <button
            className="selection-overlay__button"
            disabled={busy}
            onClick={() => { setPalette(palette === "highlight" ? null : "highlight"); setCommenting(false); }}
            type="button"
          >高亮</button>
          <button
            className="selection-overlay__button"
            disabled={busy}
            onClick={() => { setPalette(palette === "underline" ? null : "underline"); setCommenting(false); }}
            type="button"
          >下划线</button>
          <button
            className="selection-overlay__button"
            disabled={busy}
            onClick={() => { setCommenting(!commenting); setPalette(null); }}
            type="button"
          >评论</button>
          <button className="selection-overlay__button" disabled={busy} onClick={onCancel} type="button">取消</button>
        </div>
        {palette !== null && (
          <div aria-label={`${palette === "highlight" ? "高亮" : "下划线"}颜色`} className="selection-overlay__palette">
            {(palette === "highlight" ? HIGHLIGHT_COLORS : UNDERLINE_COLORS).map((color) => (
              <button
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
              placeholder="写下评论…"
              value={comment}
            />
            <button
              className="selection-overlay__button"
              disabled={comment.trim() === ""}
              onClick={() => onAnnotate?.("comment", "blue", comment.trim())}
              type="button"
            >保存评论</button>
          </div>
        )}
      </div>}
    </div>
  );
}
