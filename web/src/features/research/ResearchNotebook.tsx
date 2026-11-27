import { useEffect, useRef, useState } from "react";
import { ModalDialog } from "../ui/ModalDialog";
import { downloadText, markdownText } from "../ui/exportText";
import type { LibraryCitation } from "./AssistantPanel";
import { workspaceRequest } from "./workspaceApi";
import { coverageText, parseFullTextCoverage, studyUsageText, type FullTextCoverage } from "./fullTextStudy";

import { NotebookEditor } from "./NotebookEditor";
import { notebookDraftEvent, readNotebookDrafts, removeNotebookDraft, type NotebookDraft } from "./notebookDrafts";

type SavedCitation = LibraryCitation & { content_id?: string; source_changed?: boolean };
export type ResearchEntry = {
  id: string; title: string; question: string; note: string; starred: boolean; revision: number;
  created_at: string; updated_at: string; document_ids: string[];
  answer: { copied_from_entry_id?: string; reading_coverage?: FullTextCoverage; selection_context?: { document_id: string; content_id: string; physical_page: number; excerpt: string }; answer?: string; provider?: string; model?: string; scope?: string; citations?: SavedCitation[]; citation_status?: string; scope_documents?: { document_id: string; display_name: string; cited: boolean }[] };
};
export function notebookMarkdown(entry: ResearchEntry): string {
  const sources = (entry.answer.citations ?? []).map((c, i) => `${i + 1}. ${markdownText(c.display_name)} · 第 ${c.physical_page} 页${c.source_changed ? "（文件已变化或不可用，请重新核对）" : ""}\n   文档版本：${markdownText(c.content_id ?? "未记录")}\n   > ${markdownText(c.excerpt).replace(/\r?\n/g, "\n   > ")}`);
  return [`# ${markdownText(entry.title)}`, `记录时间：${entry.created_at}`, ...(entry.answer.copied_from_entry_id ? [`原始记录 ID：${entry.answer.copied_from_entry_id}`] : []),
    ...(entry.question ? [`## 问题\n${markdownText(entry.question)}`, `范围：${entry.answer.scope === "selected" ? `所选 ${entry.document_ids.length} 篇` : entry.answer.scope === "document" ? "单篇文献" : "整个文献库"}\n模型：${markdownText(entry.answer.provider ?? "")} / ${markdownText(entry.answer.model ?? "")}`,
      `## AI 原始回答\n${markdownText(entry.answer.answer ?? "")}`, "AI 回答仅供研读参考，请核对原文。"] : []),
    ...(entry.answer.scope_documents?.length ? [`参与研读的文献：\n${entry.answer.scope_documents.map(d => `- ${markdownText(d.display_name)}${d.cited ? "（有引用）" : "（本次回答未引用，比较未覆盖）"}`).join("\n")}`] : []),
    ...(parseFullTextCoverage(entry.answer.reading_coverage) ? [`## 全文研读范围\n${studyUsageText(entry.answer.reading_coverage!)}。原文版本覆盖：${markdownText(coverageText(entry.answer.reading_coverage!))}。仅覆盖解析文字，不含图片视觉理解；引句已核对，推论仍需核实。`] : []),
    ...(entry.answer.selection_context ? [`## 当时精读的选区 · 第 ${entry.answer.selection_context.physical_page} 页\n${markdownText(entry.answer.selection_context.excerpt)}\n\n内容版本：${markdownText(entry.answer.selection_context.content_id)}`] : []),
    `## 我的思考\n${markdownText(entry.note || "尚未填写")}`,
    ...(entry.question ? [`## 原文来源\n${sources.length ? sources.join("\n\n") : "没有可验证引用，请核对原文。"}`] : []),
  ].join("\n\n");
}

type EntrySummary = Pick<ResearchEntry, "id" | "title" | "question" | "starred" | "revision" | "created_at" | "updated_at">;
type NotebookIndex = { items: EntrySummary[]; total: number; has_more: boolean; workspace_id: string };
type Selection = { id: string; nonce: number; isNew?: boolean; draft?: NotebookDraft };
const PAGE_SIZE = 30;
function blankEntry(id: string, draft?: NotebookDraft): ResearchEntry {
  return { id, title: draft?.title ?? "新建研究笔记", note: "", starred: true, revision: 0, question: "", answer: {}, document_ids: [], created_at: new Date().toISOString(), updated_at: "" };
}
export function ResearchNotebook({ onClose, onJump, initialEntryId, initialQuery = "" }: {
  onClose(): void; onJump(citation: LibraryCitation): void; initialEntryId?: string; initialQuery?: string;
}) {
  const [index, setIndex] = useState<NotebookIndex | null>(null);
  const [query, setQuery] = useState(initialQuery);
  const [search, setSearch] = useState(initialQuery);
  const [starred, setStarred] = useState(false);
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [draftError, setDraftError] = useState(false);
  const [drafts, setDrafts] = useState<NotebookDraft[]>([]);
  const [selected, setSelected] = useState<Selection | null>(initialEntryId ? { id: initialEntryId, nonce: 0 } : null);
  const [detail, setDetail] = useState<ResearchEntry | null>(null);
  const [detailNew, setDetailNew] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [detailRetry, setDetailRetry] = useState(0);
  const [detailLoading, setDetailLoading] = useState(false);
  const leaveGuard = useRef<() => boolean>(() => true);
  const workspace = index?.workspace_id ?? "";
  useEffect(() => {
    const timer = setTimeout(() => { setSearch(query); setOffset(0); }, 250);
    return () => clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    const controller = new AbortController(); let active = true;
    setLoading(true); setError("");
    const params = new URLSearchParams({ q: search, starred: String(starred), offset: String(offset), limit: String(PAGE_SIZE) });
    workspaceRequest<NotebookIndex>(`notebook/index?${params}`, undefined, "GET", AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])).then(value => {
      if (!Array.isArray(value.items) || !/^[0-9a-f]{64}$/.test(value.workspace_id)) throw new Error("invalid_notebook");
      if (active) { setIndex(value); if (offset && !value.items.length && value.total) setOffset(Math.max(0, Math.floor((value.total - 1) / PAGE_SIZE) * PAGE_SIZE)); }
    }).catch(() => { if (active) setError("研究记录列表暂时无法读取，请重试；当前编辑内容仍保留。"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [search, starred, offset, refresh]);
  useEffect(() => {
    if (!workspace) return;
    const read = () => { try { setDrafts(readNotebookDrafts(workspace)); setDraftError(false); } catch { setDraftError(true); } };
    read(); window.addEventListener(notebookDraftEvent, read); window.addEventListener("storage", read);
    return () => { window.removeEventListener(notebookDraftEvent, read); window.removeEventListener("storage", read); };
  }, [workspace]);
  useEffect(() => {
    const controller = new AbortController(); let active = true;
    setDetail(null); setDetailError(""); setDetailNew(false);
    if (!selected || !workspace) return;
    setDetailLoading(true);
    if (selected.isNew) { setDetail(blankEntry(selected.id, selected.draft)); setDetailNew(true); setDetailLoading(false); return; }
    workspaceRequest<ResearchEntry>(`notebook/${encodeURIComponent(selected.id)}`, undefined, "GET", AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])).then(value => {
      if (active) setDetail(value);
    }).catch(reason => {
      if (!active) return;
      if (selected.draft?.isNew && reason instanceof Error && reason.message === "note_not_found") { setDetail(blankEntry(selected.id, selected.draft)); setDetailNew(true); }
      else setDetailError("这条记录暂时无法读取。可以重试，未同步的本机草稿仍保留。");
    }).finally(() => { if (active) setDetailLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [selected, workspace, detailRetry]);
  function choose(next: Selection) {
    if (!leaveGuard.current()) return;
    setSelected(next);
  }
  function close() { if (leaveGuard.current()) onClose(); }
  function discard(draft: NotebookDraft) {
    if (!window.confirm("删除这份浏览器草稿？服务器上的笔记不会被删除。")) return;
    if (!removeNotebookDraft(workspace, draft)) setDraftError(true);
  }
  return <ModalDialog label="研究记录" onClose={close}>
    <div className="notebook"><header className="notebook-heading"><div><span className="eyebrow">RESEARCH NOTEBOOK</span><h2>研究记录</h2><p>回答与来源自动留存，你的判断自动保存。</p></div><button type="button" onClick={close}>关闭研究记录</button></header>
      <div className="notebook-layout"><nav className="notebook-list" aria-label="研究记录列表">
        <button type="button" className="button button--primary" disabled={!workspace} onClick={() => choose({ id: crypto.randomUUID(), isNew: true, nonce: Date.now() })}>＋ 新建研究笔记</button>
        <input aria-label="搜索研究记录" maxLength={2000} placeholder="搜索问题、笔记、原文依据…" value={query} onChange={e => setQuery(e.target.value)} />
        <label><input type="checkbox" checked={starred} onChange={e => { setStarred(e.target.checked); setOffset(0); }} /> 只看收藏</label>
        {error && <div role="alert"><p>{error}</p><button type="button" onClick={() => setRefresh(n => n + 1)}>重新读取研究记录</button></div>}
        {loading ? <p role="status">正在读取研究记录…</p> : index && <p className="notebook-count">共 {index.total} 条 · 第 {Math.floor(offset / PAGE_SIZE) + 1} 页</p>}
        {!loading && !error && !index?.items.length && <p>暂无匹配记录。提问后会自动保存在这里，也可以新建笔记。</p>}
        {!loading && !error && <ul>{index?.items.map(item => <li key={item.id}><button type="button" aria-current={selected?.id === item.id ? "true" : undefined} onClick={() => choose({ id: item.id, nonce: Date.now() })}><strong>{item.starred ? "★ " : ""}{item.title}</strong><small>{item.question ? "问答记录" : "研究笔记"} · {new Date(item.created_at).toLocaleDateString("zh-CN")}</small></button></li>)}</ul>}
        <div className="notebook-actions"><button type="button" disabled={loading || !offset} onClick={() => setOffset(n => Math.max(0, n - PAGE_SIZE))}>上一页记录</button><button type="button" disabled={loading || !index?.has_more} onClick={() => setOffset(n => n + PAGE_SIZE)}>下一页记录</button><button type="button" disabled={loading} onClick={() => setRefresh(n => n + 1)}>刷新列表</button></div>
        {draftError && <p role="alert">浏览器草稿暂不可读取，请勿清除浏览数据；服务器上的记录仍可使用。</p>}
        {!!drafts.length && <details className="notebook-drafts"><summary>本机待同步草稿 · {drafts.length}</summary><p className="notebook-count">按工作台隔离，不自动覆盖服务器；同一记录可能有不同窗口的草稿。</p><ul>{drafts.map(draft => <li key={draft.key}><strong>{draft.title || "未命名草稿"}</strong><small>{new Date(draft.savedAt).toLocaleString()}</small><button type="button" onClick={() => choose({ id: draft.entryId, draft, nonce: Date.now() })}>恢复这份草稿</button><button type="button" onClick={() => downloadText("研究草稿.md", `# ${markdownText(draft.title)}\n\n${markdownText(draft.note)}\n\n原记录 ID：${draft.entryId}\n保存版本：${draft.revision}\n本机草稿，不代表已同步；原问答与依据需回记录查看。`)}>导出草稿</button><button type="button" onClick={() => discard(draft)}>删除草稿</button></li>)}</ul></details>}
      </nav><article className="notebook-detail" aria-label="研究记录详情">
        {detailLoading && <p role="status">正在读取这条记录与原文依据…</p>}
        {detailError && <div role="alert"><p>{detailError}</p><button type="button" onClick={() => setDetailRetry(n => n + 1)}>重试读取这条记录</button></div>}
        {detail && workspace && selected && <NotebookEditor key={`${selected.id}:${selected.nonce}`} entry={detail} isNew={detailNew} workspace={workspace} restored={selected.draft} leaveGuard={leaveGuard} onSaved={() => setRefresh(n => n + 1)} onJump={onJump} onClose={onClose} />}
        {!selected && <div className="notebook-empty"><h3>把阅读变成可回看的积累</h3><p>选择一条问答，核对原文，再记录自己的思考。</p></div>}
      </article></div>
    </div>
  </ModalDialog>;
}
