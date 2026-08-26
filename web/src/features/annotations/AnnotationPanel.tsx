import { useState } from "react";

import type { ReaderDocument } from "../reader/PdfReader";
import { annotationColorName, type AnnotationState, type PdfAnnotation } from "./annotations";


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

function typeBadge(type: string): string {
  if (type === "highlight") return "高亮摘录";
  if (type === "underline") return "下划线";
  if (type === "comment") return "思考笔记";
  return "批注";
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

  if (document === null) {
    return (
      <div className="annotation-panel-empty">
        <p>打开文献后可查看笔记</p>
      </div>
    );
  }

  const grouped = new Map<number, PdfAnnotation[]>();
  for (const item of state?.items ?? []) {
    grouped.set(item.page, [...(grouped.get(item.page) ?? []), item]);
  }

  const totalAnnotations = state?.items?.length ?? 0;

  return (
    <section aria-label="PDF 笔记" className="annotation-panel">
      <div className="annotation-panel__header">
        <div className="annotation-panel__title-group">
          <h3>文献笔记与摘录</h3>
          <span className="annotation-panel__count">{totalAnnotations} 条</span>
        </div>
        <div className="annotation-panel__global-actions">
          <button
            className="annotation-panel__action-btn"
            disabled={busy}
            onClick={onUndo}
            title="撤销上次对 PDF 的批注"
            type="button"
          >
            撤销上次
          </button>
          <button
            className="annotation-panel__action-btn"
            disabled={busy}
            onClick={onRestore}
            title="移除所有批注并恢复原版 PDF"
            type="button"
          >
            恢复原始
          </button>
        </div>
      </div>

      {busy && (
        <div className="annotation-status" role="status">
          <span className="annotation-status__spinner" />
          <span>正在写入 PDF…</span>
        </div>
      )}
      {error !== null && <p className="annotation-error" role="alert">{error}</p>}
      {!busy && state !== null && (state.items?.length ?? 0) === 0 && (
        <div className="annotation-empty-state">
          <p>尚无批注</p>
          <small>在左侧 PDF 中选中文字即可高亮、下划线或添加学术评论</small>
        </div>
      )}

      {[...grouped.entries()].sort(([left], [right]) => left - right).map(([page, items]) => (
        <div className="annotation-panel__page" key={page}>
          <div className="annotation-panel__page-label">
            <span className="annotation-panel__page-badge">第 {page} 页</span>
            <span className="annotation-panel__page-count">{items.length} 条标注</span>
          </div>
          {items.map((annotation) => (
            <article
              className="annotation-card"
              data-color={annotationColorName(annotation.color)}
              id={`annotation-${annotation.annotation_id}`}
              key={annotation.annotation_id}
            >
              <div className="annotation-card__type-bar">
                <span className="annotation-card__type-badge">{typeBadge(annotation.type)}</span>
              </div>
              <button
                className="annotation-card__jump"
                onClick={() => onJump(annotation)}
                title="点击跳转到 PDF 对应位置"
                type="button"
              >
                {label(annotation)} · {annotation.selected_text || annotation.comment || "无文字内容"}
              </button>
              {annotation.comment && (
                <div className="annotation-card__comment-box">
                  <p>{annotation.comment}</p>
                </div>
              )}
              {annotation.read_only ? (
                <small className="annotation-card__readonly">历史批注 · 只读</small>
              ) : (
                <div className="annotation-card__actions">
                  {annotation.type !== "comment" && (
                    <select
                      aria-label={`修改${label(annotation)}颜色`}
                      className="annotation-card__select"
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
                      className="annotation-card__btn"
                      disabled={busy}
                      onClick={() => { setEditing(annotation.annotation_id); setComment(annotation.comment); }}
                      type="button"
                    >
                      编辑
                    </button>
                  )}
                  <button
                    className="annotation-card__btn annotation-card__btn--delete"
                    disabled={busy}
                    onClick={() => onDelete(annotation)}
                    type="button"
                  >
                    删除
                  </button>
                </div>
              )}
              {editing === annotation.annotation_id && (
                <div className="annotation-card__editor">
                  <textarea
                    aria-label="编辑评论"
                    onChange={(event) => setComment(event.target.value)}
                    value={comment}
                  />
                  <div className="annotation-card__editor-actions">
                    <button
                      className="button button--primary button--sm"
                      disabled={busy || comment.trim() === ""}
                      onClick={() => { onUpdate(annotation, { comment: comment.trim() }); setEditing(null); }}
                      type="button"
                    >
                      保存
                    </button>
                    <button
                      className="button button--ghost button--sm"
                      onClick={() => setEditing(null)}
                      type="button"
                    >
                      取消
                    </button>
                  </div>
                </div>
              )}
            </article>
          ))}
        </div>
      ))}
    </section>
  );
}
