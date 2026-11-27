import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ModalDialog } from "../ui/ModalDialog";
import { browserDocumentApi, type DocumentApi, type DocumentListItem } from "./documents";
import { findDocuments, searchText } from "./documentSearch";
import type { ReadingRecords } from "./readingRecords";

export type QuickCommand = { id: string; title: string; description: string; keywords?: string; run(): void };
type Props = { documents: DocumentListItem[]; records: ReadingRecords; openIds: string[]; selected: string[];
  commands: QuickCommand[]; onClose(): void; onOpen(id: string): void; onCache(id: string): void;
  onSelect(ids: string[]): void; onCompare(): void; onSplit(): void;
  onDocumentsLoaded?(documents: DocumentListItem[]): void; documentApi?: DocumentApi };

export function QuickOpen({ documents, records, openIds, selected, commands, onClose, onOpen, onCache, onSelect, onCompare, onSplit, onDocumentsLoaded, documentApi = browserDocumentApi }: Props) {
  const [items, setItems] = useState(documents);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<"documents" | "commands">("documents");
  const [active, setActive] = useState<string | null>(null);
  const [limit, setLimit] = useState(60);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [notice, setNotice] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const loaded = useRef(onDocumentsLoaded); loaded.current = onDocumentsLoaded;
  const listId = useId();
  useEffect(() => {
    let valid = true; setLoading(true); setFailed(false);
    documentApi.listDocuments().then(next => { if (valid) { setItems(next); loaded.current?.(next); } })
      .catch(() => { if (valid) setFailed(true); }).finally(() => { if (valid) setLoading(false); });
    return () => { valid = false; };
  }, [documentApi, attempt]);
  useEffect(() => { setActive(null); setLimit(60); setNotice(""); }, [query, mode]);
  const results = useMemo(() => mode === "documents" ? findDocuments(items, records, query, openIds).map(doc => ({ id: doc.document_id, title: doc.filename, doc }))
    : commands.filter(c => searchText(query).split(" ").filter(Boolean).every(word => searchText(`${c.title} ${c.description} ${c.keywords ?? ""}`).includes(word))).map(command => ({ id: command.id, title: command.title, command })), [items, records, query, openIds, mode, commands]);
  const index = Math.max(0, results.findIndex(result => result.id === active));
  const current = results[index];
  const currentDoc = current && "doc" in current ? current.doc : null;
  const unavailable = selected.some(id => !items.some(d => d.document_id === id));
  useEffect(() => { window.document.getElementById(`${listId}-${index}`)?.scrollIntoView?.({ block: "nearest" }); }, [index, listId]);
  function choose() {
    if (!current) return;
    if ("doc" in current) onOpen(current.doc.document_id);
    else { onClose(); current.command.run(); }
  }
  function toggle() {
    if (!currentDoc) return;
    if (selected.includes(currentDoc.document_id)) onSelect(selected.filter(id => id !== currentDoc.document_id));
    else if (selected.length < 8) onSelect([...selected, currentDoc.document_id]);
    else setNotice("一次最多选择 8 篇，请先移出一篇。");
    input.current?.focus();
  }
  return <ModalDialog label="快速打开文献与功能" onClose={onClose}><section className="quick-open">
    <header className="quick-open__header"><div><span className="eyebrow">QUICK OPEN</span><h2>快速打开</h2></div><button className="button button--ghost" type="button" onClick={onClose}>关闭</button></header>
    <div className="quick-open__modes" role="group" aria-label="查找范围"><button type="button" aria-pressed={mode === "documents"} onClick={() => { setMode("documents"); input.current?.focus(); }}>找文献</button><button type="button" aria-pressed={mode === "commands"} onClick={() => { setMode("commands"); input.current?.focus(); }}>找功能</button><span>↑↓ 选择 · Enter 打开 · Esc 关闭</span></div>
    <input ref={input} data-autofocus role="combobox" aria-label="快速查找" aria-expanded="true" aria-controls={listId} aria-autocomplete="list" aria-activedescendant={current ? `${listId}-${index}` : undefined} placeholder={mode === "documents" ? "搜索文件名、文件夹、标签…" : "搜索阅读、笔记、图谱、备份等功能…"} value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => {
      if (e.nativeEvent.isComposing || e.keyCode === 229) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault(); const next = Math.max(0, Math.min(results.length - 1, index + (e.key === "ArrowDown" ? 1 : -1)));
        setActive(results[next]?.id ?? null); setLimit(n => Math.max(n, next + 1));
      } else if (e.key === "Enter") { e.preventDefault(); if (e.shiftKey && currentDoc) onCache(currentDoc.document_id); else choose(); }
    }} />
    {mode === "documents" && <div className="quick-open__status">{loading ? "正在刷新列表，已加载的文献仍可打开…" : failed ? <span role="alert">列表暂时无法刷新，当前显示上次加载的结果。</span> : `匹配 ${results.length} / ${items.length} 篇 · 优先显示已打开与最近阅读`}<button type="button" disabled={loading} onClick={() => setAttempt(n => n + 1)}>刷新列表</button></div>}
    <ul id={listId} role="listbox" aria-label={mode === "documents" ? "文献匹配结果" : "功能匹配结果"} className="quick-open__results">{results.slice(0, Math.max(limit, index + 1)).map((result, i) => <li key={result.id} id={`${listId}-${i}`} role="option" aria-selected={i === index} onMouseMove={() => setActive(result.id)} onMouseDown={e => e.preventDefault()} onClick={() => { if ("doc" in result) onOpen(result.doc.document_id); else { onClose(); result.command.run(); } }}>
      <span className="quick-open__kind" aria-hidden="true">{"doc" in result ? "PDF" : "→"}</span><div><strong>{result.title}</strong><small>{"doc" in result ? [result.doc.library_name ?? "文献库", result.doc.relative_folder, records[result.doc.document_id]?.lastOpened ? `第 ${records[result.doc.document_id]?.page} 页` : "", openIds.includes(result.doc.document_id) ? "已打开" : "", selected.includes(result.doc.document_id) ? "已加入对比" : "", ...(records[result.doc.document_id]?.tags ?? [])].filter(Boolean).join(" · ") : result.command.description}</small></div>
    </li>)}</ul>
    {!results.length && <p className="quick-open__empty">{mode === "commands" || items.length ? "没有匹配结果，试试更少的关键词。" : loading ? "正在读取文献…" : failed ? "列表尚未成功读取，请刷新重试。" : "还没有可打开的文献，可从文献库导入 PDF。"}</p>}
    {results.length > limit && <button className="quick-open__more" type="button" onClick={() => setLimit(n => n + 60)}>显示更多（已显示 {limit} / {results.length}）</button>}
    {mode === "documents" && <><div className="quick-open__actions"><button type="button" disabled={!currentDoc} onClick={choose}>打开阅读</button><button type="button" disabled={!currentDoc} onClick={() => currentDoc && onCache(currentDoc.document_id)}>缓存与依据 <kbd>Shift ↵</kbd></button><button type="button" disabled={!currentDoc} aria-pressed={!!currentDoc && selected.includes(currentDoc.document_id)} onClick={toggle}>{currentDoc && selected.includes(currentDoc.document_id) ? "移出对比" : "加入对比"}</button></div>
      <footer className="quick-open__selection"><span>已选 {selected.length} / 8 篇</span><button type="button" disabled={!selected.length} onClick={() => onSelect([])}>清空</button><button type="button" disabled={!selected.length || unavailable} onClick={onCompare}>研读所选文献</button><button type="button" disabled={selected.length < 2 || unavailable} onClick={onSplit}>并排前两篇</button></footer>
      {!!selected.length && <div className="quick-open__chosen">{selected.map(id => <button key={id} type="button" onClick={() => onSelect(selected.filter(d => d !== id))} title="移出对比">{items.find(d => d.document_id === id)?.filename ?? "已不可用的文献"} ×</button>)}</div>}
      {unavailable && <p className="quick-open__notice" role="status">所选范围含不可用文献，请先移除再研读。</p>}</>}
    {notice && <p className="quick-open__notice" role="status">{notice}</p>}
  </section></ModalDialog>;
}
