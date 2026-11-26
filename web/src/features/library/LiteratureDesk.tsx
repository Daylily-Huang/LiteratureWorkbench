import { useEffect, useState } from "react";
import { studyRequest, studyStatusText, type StudySummary } from "../research/StudyHistory";
import type { DocumentListItem } from "./documents";
import { documentMatches } from "./documentSearch";
import { EMPTY_READING, type ReadingRecord, type ReadingRecords } from "./readingRecords";

type Props = {
  documents: DocumentListItem[]; records: ReadingRecords; selected: string[];
  onStudy?(): void;
  onOpenSelected?(): void;
  onViewStudy?(id: string): void;
  studyRevision?: number;
  onSelect(ids: string[]): void; onOpen(id: string): void; onCompare(): void;
  onUpdate(id: string, change: (current: ReadingRecord) => ReadingRecord): void;
};
export function LiteratureDesk({ documents, records, selected, onSelect, onOpen, onCompare, onUpdate, onStudy, onOpenSelected, onViewStudy, studyRevision = 0 }: Props) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [tag, setTag] = useState("");
  const [source, setSource] = useState("");
  const [sort, setSort] = useState("recent");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [studies, setStudies] = useState<Record<string, StudySummary> | null>(null);
  const [studyError, setStudyError] = useState(false);
  const [studyRetry, setStudyRetry] = useState(0);
  const [studyFilter, setStudyFilter] = useState("all");
  useEffect(() => {
    const controller = new AbortController(); let active = true;
    setStudies(null); setStudyError(false);
    studyRequest<{ items: StudySummary[] }>("", controller.signal).then(value => {
      if (active) setStudies(Object.fromEntries(value.items.map(item => [item.document_id, item])));
    }).catch(() => { if (active) setStudyError(true); });
    return () => { active = false; controller.abort(); };
  }, [documents, studyRevision, studyRetry]);
  const tags = [...new Set(Object.values(records).flatMap(r => r.tags ?? []))].sort();
  const sources = [...new Map(documents.filter(d => d.library_id).map(d => [d.library_id!, d.library_name ?? "文献文件夹"])).entries()];
  const items = documents.filter(d => {
    const r = records[d.document_id] ?? EMPTY_READING;
    const readingStatus = r.readingStatus ?? (r.lastOpened ? "reading" : "unread");
    return documentMatches(d, records, query)
      && (status === "all" || (status === "favorite" ? r.favorite : readingStatus === status))
      && (!tag || r.tags?.includes(tag)) && (!source || d.library_id === source)
      && (studyFilter === "all" || (studies !== null && (studyFilter === "unread" ? !studies[d.document_id] : studyFilter === "history" ? !!studies[d.document_id] : studies[d.document_id]?.status === studyFilter)));
  }).sort((a, b) => sort === "name" ? a.filename.localeCompare(b.filename) : (records[b.document_id]?.lastOpened ?? 0) - (records[a.document_id]?.lastOpened ?? 0) || a.filename.localeCompare(b.filename));
  function saveTags(id: string) {
    const next = [...new Set(draft.split(/[,，]/).map(t => t.trim()).filter(Boolean))];
    if (next.length > 20 || next.some(t => t.length > 60)) { setError("最多 20 个标签，每个不超过 60 字。"); return; }
    onUpdate(id, r => ({ ...r, tags: next })); setEditing(null); setError("");
  }
  return <section className="literature-desk" aria-label="文献总览">
    <div className="desk-heading"><div><span className="eyebrow">RESEARCH LIBRARY</span><h1>我的文献</h1><p>整理阅读进度，选取论文一起研读。</p></div><span className="desk-count">{documents.length}<small>篇文献</small></span></div>
    <div className="desk-filters">
      <input aria-label="搜索文献与标签" placeholder="搜索标题、文件夹、标签…" value={query} onChange={e => setQuery(e.target.value)} />
      <select aria-label="阅读状态筛选" value={status} onChange={e => setStatus(e.target.value)}><option value="all">全部状态</option><option value="unread">未读</option><option value="reading">在读</option><option value="read">已读</option><option value="favorite">已收藏</option></select>
      <select aria-label="主题标签筛选" value={tag} onChange={e => setTag(e.target.value)}><option value="">全部标签</option>{tags.map(t => <option key={t}>{t}</option>)}</select>
      <select aria-label="总览来源筛选" value={source} onChange={e => setSource(e.target.value)}><option value="">全部文件夹</option>{sources.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select>
      <select aria-label="文献排序" value={sort} onChange={e => setSort(e.target.value)}><option value="recent">最近阅读优先</option><option value="name">按名称排序</option></select>
    </div>
    <div className="desk-study-filter"><label>AI 研读 <select aria-label="AI研读状态筛选" value={studyFilter} onChange={e => setStudyFilter(e.target.value)}><option value="all">全部研读状态</option><option value="studied">已全文研读</option><option value="stale">原文变化待重读</option><option value="history">有缓存记录</option><option value="unread">暂无研读缓存</option></select></label><button type="button" onClick={() => setStudyRetry(n => n + 1)}>刷新研读状态</button>{studyError && <span role="alert">研读状态暂不可用，请刷新重试。</span>}</div>
    <div className="desk-selection" role="group" aria-label="多篇研读选择"><span>已选 {selected.length} / 8 篇 · 筛选后 {items.length} 篇</span><button type="button" disabled={!selected.length} onClick={() => onSelect([])}>清空选择</button>{onOpenSelected && <button type="button" disabled={!selected.length} onClick={onOpenSelected}>在标签中打开所选</button>}<button className="button button--primary" type="button" disabled={!selected.length} onClick={onCompare}>研读所选文献 →</button></div>
    {onStudy && <button className="desk-study-button" type="button" onClick={onStudy}>双栏联读 · 证据矩阵 · 知识图谱 →</button>}
    {!!selected.length && <div className="desk-selected-list">{selected.map(id => <button key={id} type="button" onClick={() => onSelect(selected.filter(x => x !== id))} title="移出研读范围">{documents.find(d => d.document_id === id)?.filename ?? "已不可用的文献"} ×</button>)}</div>}
    {!items.length && <p className="desk-empty">{documents.length ? "没有匹配的文献，试着调整搜索或筛选条件。" : "从左侧导入 PDF 或关联本地文件夹，开始建立文献库。"}</p>}
    <ul className="desk-list">{items.map(d => {
      const r = records[d.document_id] ?? EMPTY_READING;
      const study = studies?.[d.document_id];
      return <li key={d.document_id} className={selected.includes(d.document_id) ? "desk-item desk-item--selected" : "desk-item"}>
        <input type="checkbox" aria-label={`选择 ${d.filename}`} checked={selected.includes(d.document_id)} disabled={!selected.includes(d.document_id) && selected.length >= 8} onChange={e => onSelect(e.target.checked ? [...selected, d.document_id] : selected.filter(id => id !== d.document_id))} />
        <div className="desk-item__body"><button className="desk-item__title" onClick={() => onOpen(d.document_id)}>{d.filename}</button><p className="desk-item__meta">{d.library_name ?? "文献库"}{d.relative_folder ? ` / ${d.relative_folder}` : ""}{r.lastOpened > 0 && ` · 上次读到第 ${r.page} 页${r.totalPages ? ` / ${r.totalPages}` : ""}`}</p>
          <div className="desk-item__tools"><button type="button" aria-label={`${r.favorite ? "取消收藏" : "收藏"} ${d.filename}`} aria-pressed={r.favorite} onClick={() => onUpdate(d.document_id, x => ({ ...x, favorite: !x.favorite }))}>{r.favorite ? "★" : "☆"}</button>
            <select aria-label={`${d.filename} 阅读状态`} value={r.readingStatus ?? (r.lastOpened ? "reading" : "unread")} onChange={e => onUpdate(d.document_id, x => ({ ...x, readingStatus: e.target.value as ReadingRecord["readingStatus"] }))}><option value="unread">未读</option><option value="reading">在读</option><option value="read">已读</option></select>
            <button type="button" className={`desk-study-badge desk-study-badge--${study?.status ?? "unread"}`} disabled={!onViewStudy} onClick={() => onViewStudy?.(d.document_id)} aria-label={`${d.filename} 研读缓存`} title={study?.reusable ? "当前模型可复用已有分析；点击查看缓存与原文依据" : "点击查看记录；重新研读需在助手中主动提问"}>{studyError ? "研读状态暂不可用" : !studies ? "读取研读状态…" : study ? `${studyStatusText[study.status]} · ${study.history_count} 条` : "暂无研读缓存"}</button>
            {(r.tags ?? []).map(t => <button className="desk-tag" key={t} onClick={() => setTag(t)}>{t}</button>)}<button type="button" onClick={() => { setEditing(d.document_id); setDraft((r.tags ?? []).join("，")); setError(""); }}>编辑标签</button>
          </div>
          {editing === d.document_id && <form className="desk-tag-editor" onSubmit={e => { e.preventDefault(); saveTags(d.document_id); }}><input autoFocus aria-label="主题标签" placeholder="用逗号分隔，例如：生态学，方法" value={draft} onChange={e => setDraft(e.target.value)} /><button type="submit">保存标签</button><button type="button" onClick={() => setEditing(null)}>取消</button>{error && <span role="alert">{error}</span>}</form>}
        </div>
      </li>;
    })}</ul>
  </section>;
}
