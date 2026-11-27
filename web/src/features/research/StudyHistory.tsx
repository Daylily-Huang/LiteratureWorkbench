import { useEffect, useRef, useState } from "react";
import { downloadText } from "../ui/exportText";
import { studyMarkdown, type CachedStudyRecord } from "./studyExport";
import { ModalDialog } from "../ui/ModalDialog";
import type { LibraryCitation } from "./AssistantPanel";
import { StudyRelationDraft, type StudyDraftReceipt } from "./StudyRelationDraft";

export type StudySummary = { document_id: string; status: "studied" | "stale" | "history" | "unavailable"; history_count: number; last_studied_at: string | null; reusable: boolean };
export const studyStatusText = { studied: "已全文研读", stale: "原文已变 · 待重读", history: "有分析 · 待核实全文", unavailable: "有历史 · 正文待就绪" };
type History = { display_name: string; total: number; has_more: boolean; items: CachedStudyRecord[]; viewedAt: string };

export async function studyRequest<T>(suffix = "", signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api/libraries/full-text/studies${suffix}`, { credentials: "same-origin", signal });
  if (!response.ok) throw new Error("study_history_unavailable");
  return response.json() as Promise<T>;
}

export function StudyHistory({ documentId, onClose, onJump, onReread, onFindNotes, onReview }: {
  documentId: string; onClose(): void; onJump(citation: LibraryCitation): void; onReread(id: string): void;
  onFindNotes?(question: string): void;
  onReview?(receipt: StudyDraftReceipt): void;
}) {
  const [history, setHistory] = useState<History | null>(null);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [exportStatus, setExportStatus] = useState("");
  const exportOwner = `${documentId}/${selected}/${history?.viewedAt}`;
  const owner = useRef(exportOwner); owner.current = exportOwner;
  useEffect(() => { setExportStatus(""); }, [exportOwner]);
  useEffect(() => {
    const controller = new AbortController(); let active = true;
    setHistory(null); setError(false); setSelected(null);
    studyRequest<History>(`/${encodeURIComponent(documentId)}?offset=${offset}`, controller.signal).then(value => {
      if (active) { setHistory({ ...value, viewedAt: new Date().toISOString() }); setSelected(value.items[0]?.cache_key ?? null); }
    }).catch(() => { if (active) setError(true); });
    return () => { active = false; controller.abort(); };
  }, [documentId, offset, retry]);
  const entry = history?.items.find(item => item.cache_key === selected);
  return <ModalDialog label="全文研读缓存" onClose={onClose}><section className="notebook study-history">
    <header className="notebook-heading"><div><span className="eyebrow">全文研读档案</span><h2>{history?.display_name ?? "缓存分析与原文依据"}</h2><p>查看本地缓存，不调用 API；引句与当前正文重新核对，AI 推论仍需你判断。</p></div><button type="button" className="button button--ghost" onClick={onClose}>关闭</button></header>
    <div className="study-history-actions"><button className="button button--primary" type="button" onClick={() => onReread(documentId)}>重新研读此篇（API）</button><small>先转到助手检查全文，再手动提问；不会立即调用。</small></div>
    {error ? <div className="notebook-detail"><p role="alert">研读缓存暂时无法读取，没有调用 AI。</p><button type="button" onClick={() => setRetry(n => n + 1)}>重试加载缓存</button></div> : !history ? <p className="notebook-detail" role="status">正在核对缓存与原文…</p> : !history.total ? <p className="notebook-empty">暂无全文研读缓存。可在研究助手中检查全文并提问。</p> : <div className="notebook-layout">
      <nav className="notebook-list" aria-label="缓存研读记录"><p className="notebook-count">共 {history.total} 条 · 每页最多 20 条</p><ul>{history.items.map(item => <li key={item.cache_key}><button type="button" aria-current={item.cache_key === selected} onClick={() => setSelected(item.cache_key)}>{item.question}<small>{new Date(item.created_at).toLocaleString()} · {item.model}</small><small>{item.invalid ? "记录未通过核验" : item.current ? "当前正文依据有效" : "历史分析 · 部分来源待核对"}</small></button></li>)}</ul><div className="notebook-actions"><button type="button" disabled={!offset} onClick={() => setOffset(n => Math.max(0, n - 20))}>上一页记录</button><button type="button" disabled={!history.has_more} onClick={() => setOffset(n => n + 20)}>下一页记录</button></div></nav>
      <article className="notebook-detail" aria-label="缓存分析详情">{entry && <><h3>{entry.question}</h3><p className="study-cache-label">缓存查看 · 本次未调用 API · {entry.model}</p>
        {entry.invalid ? <p role="alert">缓存格式或依据核验失败，暂不展示为有效分析；可手动重新研读。</p> : <>
          <div className="study-history-export"><button type="button" onClick={async () => {
            const key = owner.current;
            try { await navigator.clipboard.writeText(studyMarkdown(entry, history.viewedAt)); if (owner.current === key) setExportStatus("已复制分析与原文依据"); }
            catch { if (owner.current === key) setExportStatus("复制未成功，请使用导出保存。"); }
          }}>复制分析与依据</button><button type="button" onClick={() => downloadText(`${entry.question.slice(0, 60)}-研读分析.md`, studyMarkdown(entry, history.viewedAt))}>导出 Markdown</button><button type="button" onClick={() => downloadText(`${entry.question.slice(0, 60)}-研读记录.json`, JSON.stringify({ format: "literature-workbench-study", version: 1, viewed_at: history.viewedAt, note: "保留原研读组；核对状态以读取时为准，AI推论仍需核实。查看/导出未调用AI。", study: entry }, null, 2), "application/json;charset=utf-8")}>导出记录（JSON）</button>{onFindNotes && <button type="button" onClick={() => onFindNotes(entry.question)}>在研究记录中查找</button>}{exportStatus && <small role="status">{exportStatus}</small>}</div>
          {!entry.current && <p className="study-history-warning">这是历史分析：原组中有来源变化、不可用或版本无法核实。请勿直接当作当前有效结论。</p>}
          <details><summary>原研读范围与版本</summary><ul>{entry.documents.map((doc, index) => <li key={doc.document_id}>第 {index + 1} 篇 · {doc.display_name} · {doc.read_mode === "full_text" ? "当时读全文" : "当时复用分析/引句"} · {doc.text_status === "current" ? "正文一致" : doc.text_status === "changed" ? "正文已变化" : "版本待核对"}</li>)}</ul></details>
          {!entry.claims.length && <p>该次研读未形成有原文依据的结论。</p>}
          {entry.claims.map((claim, i) => <section className="study-cached-claim" key={i}><h3>{i + 1}. {claim.conclusion}</h3>{claim.evidence.map((e, j) => <div className="study-cached-evidence" key={j}><small>《{e.display_name}》· 第 {e.physical_page} 页 · {e.verified ? "原句已核对" : "历史引句 · 当前未核实"}</small><blockquote>{e.excerpt}</blockquote><button type="button" disabled={!e.verified} onClick={() => onJump(e)}>查看原文 · 第 {e.physical_page} 页</button></div>)}{onReview && <StudyRelationDraft key={`${entry.cache_key}/${i}`} entry={entry} claimIndex={i} onReview={onReview} />}</section>)}
          {!!entry.limitations.length && <><h3>未解决问题与适用边界</h3><ul>{entry.limitations.map((text, i) => <li key={i}>{text}</li>)}</ul></>}
        </>}
      </>}</article>
    </div>}
  </section></ModalDialog>;
}
