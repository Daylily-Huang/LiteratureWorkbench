import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import type { ReaderDocument } from "../reader/PdfReader";
import type { LibraryCitation } from "./AssistantPanel";
const PdfReader = lazy(() => import("../reader/PdfReader").then(m => ({ default: m.PdfReader })));
export type PaneLocation = { id: string; page: number; nonce: number; evidence?: LibraryCitation; expectedContentId?: string; sourceChanged?: boolean };
export type PaneProgress = { id: string; page: number; total: number };
export function LinkedPdfPane({ side, location, searchRequest, onProgress, onReady }: {
  side: "left" | "right"; location: PaneLocation | null; searchRequest?: { query: string; nonce: number };
  onProgress(side: "left" | "right", id: string, page: number, total: number): void;
  onReady(side: "left" | "right", document: ReaderDocument | null): void;
}) {
  const [document, setDocument] = useState<ReaderDocument | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [loadedFor, setLoadedFor] = useState("");
  const loadKey = `${location?.id}:${location?.nonce}:${attempt}`;
  useEffect(() => {
    setDocument(null); setError(false); onReady(side, null);
    if (!location) return;
    const id = location.id;
    const controller = new AbortController();
    void fetch(`/api/documents/${encodeURIComponent(id)}`, { credentials: "same-origin", signal: controller.signal }).then(async r => {
      if (!r.ok) throw new Error("document_unavailable");
      const d = await r.json();
      if (!d.content_id || !d.current_document_version_id) throw new Error("document_unavailable");
      if (controller.signal.aborted) return;
      const value = { documentId: d.document_id, documentVersionId: d.current_document_version_id, contentId: d.content_id, filename: d.filename };
      setDocument(value); onReady(side, value);
      setLoadedFor(loadKey);
    }).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [location?.id, location?.nonce, side, onReady, attempt]);
  const currentDocument = loadedFor === loadKey ? document : null;
  const report = useCallback((id: string, page: number, total: number) => onProgress(side, id, page, total), [side, onProgress]);
  const evidence = location?.evidence?.document_id === location?.id ? location?.evidence : undefined;
  const expected = evidence?.content_id ?? location?.expectedContentId;
  const changed = location?.sourceChanged || evidence?.source_changed || (!!expected && expected !== currentDocument?.contentId);
  const matched = !!expected && expected === currentDocument?.contentId && !changed;
  const highlight = evidence && matched && currentDocument ? { documentId: currentDocument.documentId, page: location!.page, rects: evidence.rects, nonce: location!.nonce } : null;
  return <div className="linked-pdf-pane" aria-label={side === "left" ? "左侧原文" : "右侧原文"}>
    {currentDocument?.documentId === location?.id && currentDocument && (evidence || expected) && <details className="linked-evidence" open={!!changed}><summary>证据定位 · 第 {location?.page} 页 · {changed ? "来源已变化" : matched ? "版本一致" : "版本未记录"}</summary>
      <p>{changed ? "证据关联的来源已变化或不可用，仅定位当前页码，不套用历史高亮。请重新核对。" : matched ? "以下为保存时的原文摘录，可与本页高亮对照。" : "以下为历史摘录，未记录版本，不套用高亮坐标。"}</p>
      {evidence?.excerpt && <blockquote>{evidence.excerpt}</blockquote>}
    </details>}
    {!location ? <div className="studio-empty"><h3>{side === "left" ? "左侧原文" : "右侧原文"}</h3><p>从上方选择论文，开始并排对照。</p></div> : error ? <div role="alert"><p>原文暂时无法读取，请核对文件是否仍在。</p><button onClick={() => setAttempt(n => n + 1)}>重新读取原文</button></div> : currentDocument?.documentId === location.id ? <Suspense fallback={<p>正在加载阅读器…</p>}><PdfReader key={currentDocument.documentId} {...currentDocument} selectionTools={false} citationHighlight={highlight} fitOnOpen initialPage={location.page} requestedPage={location.page} pageRequestId={location.nonce} searchRequest={searchRequest} onProgress={report} /></Suspense> : <p role="status">正在读取文献…</p>}
  </div>;
}
