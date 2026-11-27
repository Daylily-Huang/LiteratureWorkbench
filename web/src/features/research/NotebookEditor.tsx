import { useEffect, useRef, useState, type RefObject } from "react";
import { downloadText } from "../ui/exportText";
import type { LibraryCitation } from "./AssistantPanel";
import { parseFullTextCoverage, StudyCoverage } from "./fullTextStudy";
import { notebookMarkdown, type ResearchEntry } from "./ResearchNotebook";
import { sameNote, writeNotebookDraft, removeNotebookDraft, type NotebookDraft, type NoteFields } from "./notebookDrafts";
import { workspaceRequest } from "./workspaceApi";

type Props = { entry: ResearchEntry; isNew: boolean; workspace: string; restored?: NotebookDraft;
  onSaved(entry: ResearchEntry): void; onJump(citation: LibraryCitation): void; onClose(): void; leaveGuard: RefObject<() => boolean> };
export function NotebookEditor({ entry, isNew, workspace, restored, onSaved, onJump, onClose, leaveGuard }: Props) {
  const initial = restored ?? entry;
  const [fields, setFields] = useState<NoteFields>({ title: initial.title, note: initial.note, starred: initial.starred });
  const [base, setBase] = useState(entry);
  const [creating, setCreating] = useState(isNew);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(!!restored && !isNew && restored.revision !== entry.revision && !sameNote(restored, entry));
  const [notice, setNotice] = useState(restored ? "已恢复本机草稿，正在核对保存版本。" : "");
  const [storageFailed, setStorageFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const key = useRef(crypto.randomUUID());
  const copyId = useRef(crypto.randomUUID());
  const revision = useRef(restored?.revision ?? entry.revision);
  const resumed = useRef(restored);
  const localDraft = useRef<NotebookDraft | null>(null);
  const mounted = useRef(true), requestLock = useRef(false);
  const latest = useRef({ fields, base, creating, conflict }); latest.current = { fields, base, creating, conflict };
  const dirty = !sameNote(fields, base) || !!restored && creating;
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const savingRef = useRef<() => Promise<void>>(async () => {});

  function persist(value: NoteFields, baseline = latest.current.base, newEntry = latest.current.creating) {
    const draft: NotebookDraft = { version: 1, key: key.current, entryId: entry.id, revision: newEntry ? baseline.revision : revision.current,
      isNew: newEntry, title: value.title, note: value.note, starred: value.starred, savedAt: new Date().toISOString() };
    const okay = writeNotebookDraft(workspace, draft);
    if (okay) localDraft.current = draft;
    if (mounted.current) setStorageFailed(!okay);
    return okay;
  }
  function change(value: Partial<NoteFields>) {
    const updated = { ...latest.current.fields, ...value };
    latest.current.fields = updated;
    // Write ahead of render/debounce so immediate close or switching cannot lose typed text.
    if (sameNote(updated, latest.current.base)) {
      if (localDraft.current) removeNotebookDraft(workspace, localDraft.current);
      localDraft.current = null;
    } else persist(updated);
    setFields(updated); setNotice(""); setError("");
  }
  async function reconcile(snapshot: NoteFields) {
    const current = await workspaceRequest<ResearchEntry>(`notebook/${entry.id}`, undefined, "GET", AbortSignal.timeout(8000));
    return sameNote(snapshot, current) ? current : null;
  }
  async function save() {
    const { fields: snapshot, base: expected, creating: newEntry, conflict: blocked } = latest.current;
    if (requestLock.current || blocked || !snapshot.title.trim() || (!dirtyRef.current && !newEntry)) return;
    requestLock.current = true; setSaving(true); setError("");
    const draft = localDraft.current;
    const originalDraft = resumed.current;
    let saved: ResearchEntry | null = null;
    try {
      if (newEntry) {
        const result = await workspaceRequest<{ id: string; revision: number; created_at: string; starred: boolean }>("notebook", { title: snapshot.title.trim(), note: snapshot.note, starred: snapshot.starred, note_id: entry.id }, "POST", AbortSignal.timeout(15000));
        saved = { ...expected, ...snapshot, title: snapshot.title.trim(), starred: result.starred, revision: result.revision, created_at: result.created_at };
      } else {
        const result = await workspaceRequest<{ revision: number }>(`notebook/${entry.id}`, { title: snapshot.title.trim(), note: snapshot.note, starred: snapshot.starred, revision: expected.revision }, "PATCH", AbortSignal.timeout(15000));
        saved = { ...expected, ...snapshot, title: snapshot.title.trim(), revision: result.revision, updated_at: new Date().toISOString() };
      }
    } catch (reason) {
      // A response can be lost after commit. A read-only equality check may acknowledge
      // that save; it must never retry an overwrite using a newer revision.
      try { saved = await reconcile(snapshot); } catch { /* Keep the write-ahead draft. */ }
      if (!saved && mounted.current) {
        const code = reason instanceof Error ? reason.message : "";
        if (code === "note_conflict") setConflict(true);
        setError(code === "note_conflict" ? "这条记录已在其他窗口修改。当前草稿仍在，可另存副本或载入服务器版本核对。" : "暂未同步到工作台。请保留当前浏览器，联网后重试，或先导出草稿。");
      }
    } finally {
      if (saved) {
        if (draft) removeNotebookDraft(workspace, draft);
        if (originalDraft) removeNotebookDraft(workspace, originalDraft);
        if (mounted.current) {
          const currentFields = latest.current.fields;
          latest.current = { ...latest.current, base: saved, creating: false };
          revision.current = saved.revision; resumed.current = undefined;
          setBase(saved); setCreating(false);
          if (!sameNote(currentFields, saved)) persist(currentFields, saved, false);
          else { setFields({ title: saved.title, note: saved.note, starred: saved.starred }); localDraft.current = null; }
          setNotice(sameNote(currentFields, saved) ? "已自动保存到研究记录" : "此前修改已保存，正在继续同步新输入。"); setError(""); setConflict(false); onSaved(saved);
        }
      }
      requestLock.current = false;
      if (mounted.current) { setSaving(false); setRetry(n => n + 1); }
    }
  }
  savingRef.current = save;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (!dirty || conflict || error || !fields.title.trim()) return;
    const timer = setTimeout(() => void savingRef.current(), 1000);
    return () => clearTimeout(timer);
  }, [fields, base.revision, dirty, conflict, error, retry]);
  useEffect(() => {
    if (restored && !isNew && sameNote(restored, entry)) { removeNotebookDraft(workspace, restored); resumed.current = undefined; setNotice("草稿内容已在工作台保存，无需重复写入。"); }
    else if (restored) persist(restored);
  }, []);
  useEffect(() => {
    const guard = () => sameNote(latest.current.fields, latest.current.base) || persist(latest.current.fields) || window.confirm("草稿未能保存在浏览器中，请先导出；仍然离开吗？");
    leaveGuard.current = guard;
    return () => { if (leaveGuard.current === guard) leaveGuard.current = () => true; };
  }, []);
  useEffect(() => {
    const online = () => { if (!latest.current.conflict && dirtyRef.current) { setError(""); void savingRef.current(); } };
    const unload = (event: BeforeUnloadEvent) => { if (dirtyRef.current) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("online", online); window.addEventListener("beforeunload", unload);
    return () => { window.removeEventListener("online", online); window.removeEventListener("beforeunload", unload); };
  }, []);
  useEffect(() => {
    if (!conflict) return;
    setError("这条记录已在其他窗口修改。当前草稿仍在，可另存副本或载入服务器版本核对。");
  }, [conflict]);

  function jump(citation: LibraryCitation) {
    if (dirty && !persist(fields) && !window.confirm("草稿未能保存在浏览器中，请先导出；仍然离开吗？")) return;
    if (citation.source_changed && !window.confirm("原文文件已变化或不可用，保存的页码可能不再对应。仍然尝试打开当前文件吗？")) return;
    if (dirty && !conflict) void save(); onClose(); onJump(citation);
  }
  async function copyDraft() {
    if (requestLock.current || !fields.title.trim()) return;
    requestLock.current = true; setSaving(true); setError("");
    try {
      const result = await workspaceRequest<ResearchEntry>(`notebook/${entry.id}/copy`, { title: fields.title.trim().slice(0, 113) + "（草稿副本）", note: fields.note, note_id: copyId.current }, "POST", AbortSignal.timeout(15000));
      setNotice("已另存为独立笔记，原问答和其他窗口的修改均保留。");
      onSaved(result);
    } catch (reason) {
      if (mounted.current) {
        if (reason instanceof Error && reason.message === "note_conflict") {
          copyId.current = crypto.randomUUID();
          setError("之前的副本已存在且内容不同，已保留。再次另存会创建新副本，也可以先导出当前草稿。");
        } else setError("另存未成功，当前草稿仍在，请重试或导出。");
      }
    }
    finally { requestLock.current = false; if (mounted.current) setSaving(false); }
  }
  async function reloadSaved() {
    if (requestLock.current) return;
    if (!persist(latest.current.fields)) { setError("本机草稿未能保存，请先导出当前草稿，再载入服务器版本。"); return; }
    if (!window.confirm("将显示服务器上的最新版本。本机草稿会保留，可稍后恢复或导出。")) return;
    const startingFields = latest.current.fields;
    requestLock.current = true; setSaving(true);
    try {
      const current = await workspaceRequest<ResearchEntry>(`notebook/${entry.id}`, undefined, "GET", AbortSignal.timeout(15000));
      if (mounted.current && latest.current.fields !== startingFields) { setNotice("读取期间又有新输入，未替换当前草稿。请核对后再载入服务器版本。"); return; }
      if (mounted.current) { key.current = crypto.randomUUID(); localDraft.current = null; resumed.current = undefined; revision.current = current.revision; latest.current = { fields: current, base: current, creating: false, conflict: false }; setFields(current); setBase(current); setCreating(false); setConflict(false); setError(""); setNotice("已载入服务器版本；本机草稿仍保留。"); }
    } catch { if (mounted.current) setError("最新版本暂时无法读取，本机草稿仍保留。"); }
    finally { requestLock.current = false; if (mounted.current) setSaving(false); }
  }
  const exported = { ...base, ...fields };
  return <>
    {error && <p role="alert">{error}</p>}
    {storageFailed && <p role="alert">浏览器草稿存储不可用。关闭或刷新前请确认已保存，或导出当前草稿。</p>}
    {notice && <p role="status">{notice}</p>}
    <label className="notebook-field">标题<input aria-label="笔记标题" maxLength={120} value={fields.title} onChange={e => change({ title: e.target.value })} /></label>
    {base.answer.copied_from_entry_id && <p className="notebook-count">本记录另存自原记录 {base.answer.copied_from_entry_id}，当时的问答与原文依据随副本保留。</p>}
    {!creating && <label><input type="checkbox" checked={fields.starred} onChange={e => change({ starred: e.target.checked })} /> 收藏这条记录</label>}
    {!!base.answer.scope_documents?.length && <details><summary>当时选择的文献 · {base.document_ids.length} 篇</summary><ul>{base.answer.scope_documents.map(d => <li key={d.document_id}>{d.display_name} · {d.cited ? "有引用" : "本次回答未引用，比较未覆盖"}</li>)}</ul></details>}
    {base.question && <><h3>当时的问题</h3><p>{base.question}</p><p className="notebook-count">{base.answer.scope === "selected" ? `选定 ${base.document_ids.length} 篇文献` : base.answer.scope === "document" ? "当前文献" : "整个文献库"} · {base.answer.provider} / {base.answer.model}</p><details className="notebook-original"><summary>查看 AI 原始回答与原文依据</summary><h3>AI 原始回答</h3>{parseFullTextCoverage(base.answer.reading_coverage) && <StudyCoverage study={base.answer.reading_coverage!} sent />}{base.answer.selection_context && <details className="assistant-passage"><summary>当时精读的选区 · 第 {base.answer.selection_context.physical_page} 页</summary><blockquote>{base.answer.selection_context.excerpt}</blockquote><small>保留的是提问时的原文；来源状态见下方引用。</small></details>}<p className="notebook-answer">{base.answer.answer}</p><p className="notebook-count">原始回答保留不变；补充与修正请写在「我的思考」中。</p>
      {!base.answer.citations?.length && <p className="assistant-answer__missing-hint">没有可验证的原文引用，请核对原文。</p>}
      <div className="notebook-sources">{base.answer.citations?.map((c, i) => <details key={`${c.document_id}:${i}`}><summary>{c.display_name} · 第 {c.physical_page} 页{c.source_changed ? " · 来源已变化" : ""}</summary><blockquote>{c.excerpt}</blockquote>{c.source_changed && <p role="note">文件已变化或不可用。这里展示的是回答当时保存的原文片段，请重新核对。</p>}<button type="button" onClick={() => jump(c)}>回到原文第 {c.physical_page} 页</button></details>)}</div>
    </details></>}
    <label className="notebook-field">我的思考<textarea aria-label="我的思考" maxLength={50000} rows={8} placeholder="记录你的判断、局限、下一步验证计划…" value={fields.note} onChange={e => change({ note: e.target.value })} /></label>
    <p className="notebook-count">停止输入后自动保存。未同步草稿保留在当前浏览器地址下；切换浏览器、端口或清除浏览数据后不能保证恢复，请先保存或导出。</p>
    <div className="notebook-actions"><button className="button button--primary" type="button" disabled={saving || conflict || !fields.title.trim() || !dirty} onClick={() => { setError(""); void save(); }}>{saving ? "保存中…" : error ? "重试保存" : "保存笔记"}</button><button type="button" onClick={() => downloadText("研究笔记.md", notebookMarkdown(exported))}>导出{dirty ? "当前草稿" : "笔记与来源"}</button><span role="status">{saving ? "正在同步到工作台…" : dirty ? storageFailed ? "有待同步修改 · 本机草稿未能保存" : "有待同步修改 · 本机草稿保留" : ""}</span></div>
    {conflict && <div className="notebook-actions"><button type="button" disabled={saving} onClick={() => void copyDraft()}>另存为新笔记</button><button type="button" disabled={saving} onClick={() => void reloadSaved()}>载入服务器版本</button></div>}
  </>;
}
