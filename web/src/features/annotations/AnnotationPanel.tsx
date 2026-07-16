import { useState } from "react";

import type { ReaderDocument } from "../reader/PdfReader";
import type { AnnotationState, PdfAnnotation } from "./annotations";


type AnnotationPanelProps = {
  document: ReaderDocument | null;
  state: AnnotationState | null;
  busy: boolean;
  error: string | null;
  onDelete(annotation: PdfAnnotation): void;
  onJump(annotation: PdfAnnotation): void;
  onRestore(): void;
  onUndo(): void;
  onUpdate(annotation: PdfAnnotation, changes: { color?: string; comment?: string }): void;
};

const HIGHLIGHT_COLORS = ["yellow", "green", "blue", "pink"];
const UNDERLINE_COLORS = ["red", "blue", "black"];


function label(annotation: PdfAnnotation): string {
  if (annotation.type === "highlight") return "高亮";
  if (annotation.type === "underline") return "下划线";
  if (annotation.type === "comment") return "评论";
  return annotation.type || "PDF 批注";
}


export function AnnotationPanel({
  document,
  state,
  busy,
  error,
  onDelete,
  onJump,
  onRestore,
  onUndo,
  onUpdate,
}: AnnotationPanelProps) {
  const [editing, setEditing] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  if (document === null) return <p>打开文献后可查看笔记</p>;
  const grouped = new Map<number, PdfAnnotation[]>();
  for (const item of state?.items ?? []) {
    grouped.set(item.page, [...(grouped.get(item.page) ?? []), item]);
  }
  return (
    <section aria-label="PDF 笔记" className="annotation-panel">
      <div className="annotation-panel__header">
        <h3>PDF 笔记</h3>
        <div>
          <button disabled={busy} onClick={onUndo} type="button">撤销上次</button>
          <button disabled={busy} onClick={onRestore} type="button">恢复原始</button>
        </div>
      </div>
      {busy && <p role="status">正在写入 PDF…</p>}
      {error !== null && <p role="alert">{error}</p>}
      {!busy && state !== null && state.items.length === 0 && <p>尚无批注</p>}
      {[...grouped.entries()].sort(([left], [right]) => left - right).map(([page, items]) => (
        <div className="annotation-panel__page" key={page}>
          <strong>第 {page} 页</strong>
          {items.map((annotation) => (
            <article className="annotation-card" id={`annotation-${annotation.annotation_id}`} key={annotation.annotation_id}>
              <button className="annotation-card__jump" onClick={() => onJump(annotation)} type="button">
                {label(annotation)} · {annotation.selected_text || annotation.comment || "无文字内容"}
              </button>
              {annotation.comment && <p>{annotation.comment}</p>}
              {annotation.read_only ? (
                <small>历史批注 · 只读</small>
              ) : (
                <div className="annotation-card__actions">
                  {annotation.type !== "comment" && (
                    <select
                      aria-label={`修改${label(annotation)}颜色`}
                      disabled={busy}
                      onChange={(event) => onUpdate(annotation, { color: event.target.value })}
                      defaultValue={annotation.type === "underline" ? "red" : "yellow"}
                    >
                      {(annotation.type === "underline" ? UNDERLINE_COLORS : HIGHLIGHT_COLORS).map((color) => (
                        <option key={color} value={color}>{color}</option>
                      ))}
                    </select>
                  )}
                  {annotation.type === "comment" && editing !== annotation.annotation_id && (
                    <button
                      disabled={busy}
                      onClick={() => { setEditing(annotation.annotation_id); setComment(annotation.comment); }}
                      type="button"
                    >编辑</button>
                  )}
                  <button disabled={busy} onClick={() => onDelete(annotation)} type="button">删除</button>
                </div>
              )}
              {editing === annotation.annotation_id && (
                <div className="annotation-card__editor">
                  <textarea aria-label="编辑评论" onChange={(event) => setComment(event.target.value)} value={comment} />
                  <button
                    disabled={busy || comment.trim() === ""}
                    onClick={() => { onUpdate(annotation, { comment: comment.trim() }); setEditing(null); }}
                    type="button"
                  >保存</button>
                  <button onClick={() => setEditing(null)} type="button">取消</button>
                </div>
              )}
            </article>
          ))}
        </div>
      ))}
    </section>
  );
}
