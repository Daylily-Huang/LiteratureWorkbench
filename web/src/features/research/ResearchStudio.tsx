import { useCallback, useEffect, useRef, useState } from "react";
import type { DocumentListItem } from "../library/documents";
import type { ReadingRecord, ReadingRecords } from "../library/readingRecords";
import type { ReaderDocument } from "../reader/PdfReader";
import type { LibraryCitation } from "./AssistantPanel";
import { ModalDialog } from "../ui/ModalDialog";
import { RelationReview } from "./RelationReview";
import { QuestionEvidence } from "./QuestionEvidence";
import { DIFFERENCE_LABELS, questionKey } from "./graphModel";
import { EvidenceMatrix, type EvidenceDocument } from "./EvidenceMatrix";
import { KnowledgeGraph } from "./KnowledgeGraph";
import { LinkedPdfPane, type PaneLocation, type PaneProgress } from "./LinkedPdfPane";
import { ResearchNotebook } from "./ResearchNotebook";
import { StructuredComparison } from "./StructuredComparison";
import { workspaceRequest } from "./workspaceApi";
import type { GraphData, GraphEdge, ReadingGroup } from "./graphModel";

type Side = "left" | "right";
const EMPTY_GRAPH: GraphData = { nodes: [], edges: [], history_truncated: false };
export function alignedPage(page: number, total: number, otherTotal: number) {
  if (total <= 1 || otherTotal <= 1) return 1;
  return Math.max(1, Math.min(otherTotal, Math.round((page - 1) / (total - 1) * (otherTotal - 1)) + 1));
}
function studioError(reason: unknown) {
  const code = reason instanceof Error ? reason.message : "";
  return ({ group_conflict: "研读组已被其他窗口修改。请重新读取后另存，避免覆盖。", document_not_found: "文献已不可用，请从研读组中移除并重新选择。", source_changed: "原文已发生变化。请重新打开两侧原文，核对后再记录关系。", page_unavailable: "页码尚未确认，请等待解析完成或核对实际页数。" } as Record<string, string>)[code] ?? "操作未完成，请保留当前内容并重试。";
}

export function ResearchStudio({ documents, initialIds, initialAnalysisId, records, onUpdate, onAsk, onClose, onSolo }: {
  documents: DocumentListItem[]; initialIds: string[]; initialAnalysisId?: string; records: ReadingRecords;
  onUpdate(id: string, change: (current: ReadingRecord) => ReadingRecord): void;
  onAsk(ids: string[], question: string): void; onClose(): void; onSolo?(id: string, page: number): void;
}) {
  const [ids, setIds] = useState(initialIds.slice(0, 8));
  const [groups, setGroups] = useState<ReadingGroup[]>([]);
  const [groupId, setGroupId] = useState("");
  const [groupBaseline, setGroupBaseline] = useState<ReadingGroup | null>(null);
  const [title, setTitle] = useState("");
  const [mode, setMode] = useState<"reading" | "matrix" | "graph" | "questions" | "ai" | "comparison">(initialAnalysisId ? "ai" : "reading");
  const [scopeAll, setScopeAll] = useState(!initialIds.length);
  const [comparisonOpened, setComparisonOpened] = useState(false);
  const [graph, setGraph] = useState<GraphData>(EMPTY_GRAPH);
  const [questions, setQuestions] = useState<string[]>([]);
  const [evidenceRefresh, setEvidenceRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [analysisDirty, setAnalysisDirty] = useState(false);
  const [notebook, setNotebook] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [searchRequest, setSearchRequest] = useState<{ query: string; nonce: number }>();
  const [relation, setRelation] = useState("supports");
  const [relationNote, setRelationNote] = useState("");
  const [relationQuestion, setRelationQuestion] = useState("");
  const [differenceBasis, setDifferenceBasis] = useState("unspecified");
  const [comparisonNote, setComparisonNote] = useState("");
  const [questionFocus, setQuestionFocus] = useState("");
  const relationDirty = !!(relationNote.trim() || relationQuestion.trim() || comparisonNote.trim()) || differenceBasis !== "unspecified";
  function clearRelation() { setRelationNote(""); setRelationQuestion(""); setDifferenceBasis("unspecified"); setComparisonNote(""); }

  const [activeSide, setActiveSide] = useState<Side>("left");
  const [positions, setPositions] = useState<Partial<Record<Side, PaneProgress>>>({});
  const [metadata, setMetadata] = useState<Partial<Record<Side, ReaderDocument | null>>>({});
  const [panes, setPanes] = useState<Record<Side, PaneLocation | null>>(() => ({ left: initialIds[0] ? { id: initialIds[0], page: records[initialIds[0]]?.page ?? 1, nonce: 0 } : null, right: initialIds[1] ? { id: initialIds[1], page: records[initialIds[1]]?.page ?? 1, nonce: 0 } : null }));
  const panesRef = useRef(panes); panesRef.current = panes;
  const positionsRef = useRef(positions); positionsRef.current = positions;
  const group = groupBaseline;
  const groupDirty = group ? title !== group.title || JSON.stringify(ids) !== JSON.stringify(group.document_ids) : !!title.trim();
  const dirty = relationDirty || groupDirty || analysisDirty;
  function canLeave() { return !dirty || window.confirm("有尚未保存的研读组、关系说明或 AI 审核草稿，继续离开吗？"); }
  function close() { if (!busy && canLeave()) onClose(); }
  function switchMode(next: "reading" | "matrix" | "graph" | "questions" | "ai" | "comparison") {
    if (mode === "reading" && next !== "reading") {
      setPanes(current => ({
        left: current.left ? { ...current.left, page: positions.left?.id === current.left.id ? positions.left.page : current.left.page } : null,
        right: current.right ? { ...current.right, page: positions.right?.id === current.right.id ? positions.right.page : current.right.page } : null,
      }));
    }
    setMode(next);
  }
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  async function refresh() {
    setLoading(true); setError("");
    try {
      const [g, network, notebook] = await Promise.all([workspaceRequest<{ items: ReadingGroup[] }>("groups"), workspaceRequest<GraphData>("graph"), workspaceRequest<{ items: { question: string }[] }>("notebook/index?limit=100")]);
      setGroups(g.items); setGraph(network); setQuestions(notebook.items.map(entry => entry.question)); setEvidenceRefresh(value => value + 1);
    } catch (e) { setError(studioError(e)); }
    finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, []);
  const report = useCallback((side: Side, id: string, page: number, total: number) => {
    if (panesRef.current[side]?.id !== id) return;
    const previous = positionsRef.current[side];
    if (previous?.id === id && previous.page === page && previous.total === total) return;
    setPositions(current => ({ ...current, [side]: { id, page, total } }));
    onUpdate(id, current => ({ ...current, page, totalPages: total, lastOpened: Date.now(), readingStatus: current.readingStatus === "read" ? "read" : "reading" }));
  }, [onUpdate]);
  const ready = useCallback((side: Side, document: ReaderDocument | null) => {
    if (document && panesRef.current[side]?.id !== document.documentId) return;
    setMetadata(current => ({ ...current, [side]: document }));
  }, []);
  function open(id: string, side: Side, page?: number, evidence?: LibraryCitation) {
    if ((relationDirty || analysisDirty) && !window.confirm("切换原文会清空尚未保存的关系说明，继续吗？")) return;
    if (!ids.includes(id) && ids.length >= 8) { setError("研读组最多 8 篇，请先移除一篇再加入。当前选择保持不变。"); return; }
    clearRelation(); setError(""); setMode("reading");
    if (!ids.includes(id)) setIds(current => [...current, id]);
    setPositions(current => ({ ...current, [side]: undefined }));
    setPanes(current => ({ ...current, [side]: { id, page: page ?? records[id]?.page ?? 1, nonce: (current[side]?.nonce ?? 0) + 1, evidence } }));
    setActiveSide(side);
  }
  function loadGroup(id: string) {
    if (busy || !canLeave()) return;
    const selected = groups.find(g => g.id === id);
    setGroupId(id); setGroupBaseline(selected ?? null); setTitle(selected?.title ?? ""); setIds(selected?.document_ids ?? []); clearRelation(); setError(""); setScopeAll(false);
    setPositions({}); setPanes({ left: selected?.document_ids[0] ? { id: selected.document_ids[0], page: records[selected.document_ids[0]]?.page ?? 1, nonce: 0 } : null, right: selected?.document_ids[1] ? { id: selected.document_ids[1], page: records[selected.document_ids[1]]?.page ?? 1, nonce: 0 } : null });
  }
  async function refreshSavedGraph() {
    try { setGraph(await workspaceRequest<GraphData>("graph")); }
    catch { setError("操作已保存，但关联视图暂未刷新。请点击“刷新关联”，无需重复保存。"); }
  }
  async function saveGroup(asNew = false) {
    if (busy || !ids.length || !title.trim()) return;
    setBusy(true); setError("");
    try {
      const result = await workspaceRequest<{ id: string; revision: number }>(group && !asNew ? `groups/${group.id}` : "groups", { title: title.trim(), document_ids: ids, ...(group && !asNew ? { revision: group.revision } : {}) }, group && !asNew ? "PATCH" : "POST");
      const value = { id: result.id, revision: result.revision, title: title.trim(), document_ids: [...ids] };
      setGroups(current => [value, ...current.filter(g => g.id !== value.id)]); setGroupId(value.id); setGroupBaseline(value); setTitle(value.title); setNotice("研读组已保存，可以下次继续。");
      await refreshSavedGraph();
    } catch (e) { setError(studioError(e)); }
    finally { setBusy(false); }
  }
  async function removeGroup() {
    if (!group || busy || !window.confirm("删除这个研读组？文献、笔记和文献关系会保留。")) return;
    setBusy(true);
    try { await workspaceRequest(`groups/${group.id}`, {}, "DELETE"); setGroups(current => current.filter(g => g.id !== group.id)); setGroupId(""); setGroupBaseline(null); setTitle(""); setNotice("研读组已删除，文献仍保留。"); await refreshSavedGraph(); }
    catch (e) { setError(studioError(e)); } finally { setBusy(false); }
  }
  function removeDocument(id: string) {
    if ((relationDirty || analysisDirty) && !window.confirm("移除文献会清空尚未保存的关系说明，继续吗？")) return;
    clearRelation(); setIds(current => current.filter(x => x !== id));
    setPanes(current => ({ left: current.left?.id === id ? null : current.left, right: current.right?.id === id ? null : current.right }));
  }
  const left = positions.left?.id === panes.left?.id ? positions.left : undefined;
  const right = positions.right?.id === panes.right?.id ? positions.right : undefined;
  const leftMeta = metadata.left?.documentId === panes.left?.id ? metadata.left : null;
  const rightMeta = metadata.right?.documentId === panes.right?.id ? metadata.right : null;
  async function saveRelation() {
    if (busy || !left || !right || !leftMeta || !rightMeta || left.id === right.id || !relationNote.trim() || !relationQuestion.trim()) return;
    setBusy(true); setError("");
    try {
      await workspaceRequest("relations", { source_id: left.id, target_id: right.id, source_page: left.page, target_page: right.page, source_content_id: leftMeta.contentId, target_content_id: rightMeta.contentId, relation, note: relationNote.trim(), question: relationQuestion.trim(), difference_basis: differenceBasis, comparison_note: comparisonNote.trim() });
      clearRelation(); setNotice("文献关系已保存到图谱（手动判断，尚需核对）。"); await refreshSavedGraph();
    } catch (e) { setError(studioError(e)); } finally { setBusy(false); }
  }
  async function deleteRelation(id: string) {
    if (busy || !window.confirm("删除这条手动关系？原始论文和问答会保留。")) return;
    setBusy(true); setError("");
    try { await workspaceRequest(`relations/${id}`, {}, "DELETE"); await refreshSavedGraph(); }
    catch (e) { setError(studioError(e)); } finally { setBusy(false); }
  }
  function pair(edge: GraphEdge) {
    if (!canLeave()) return;
    const a = edge.source.replace(/^doc:/, ""); const b = edge.target.replace(/^doc:/, "");
    const next = [...new Set([...ids, a, b])];
    if (next.length > 8) { setError("请先从当前研读组移除文献，为这两篇留出位置。"); return; }
    setIds(next); clearRelation(); setPositions({}); setPanes(current => ({
      left: { id: a, page: edge.source_page ?? 1, nonce: (current.left?.nonce ?? 0) + 1, evidence: edge.evidence?.source, expectedContentId: edge.source_content_id, sourceChanged: edge.source_changed },
      right: { id: b, page: edge.target_page ?? 1, nonce: (current.right?.nonce ?? 0) + 1, evidence: edge.evidence?.target, expectedContentId: edge.target_content_id, sourceChanged: edge.source_changed },
    })); setMode("reading");
  }
  function ask() { if (!busy && canLeave()) onAsk(ids, "请比较这组文献的研究问题、方法、关键证据与分歧。逐项给出来源，区分原文证据、个人解释和未覆盖内容。"); }
  const selectedDocuments = ids.map(id => documents.find(d => d.document_id === id)).filter((d): d is DocumentListItem => !!d);
  const evidenceDocuments: EvidenceDocument[] = ids.map(id => documents.find(d => d.document_id === id) ?? { document_id: id, filename: `已不可用的文献（${id.slice(0, 8)}）`, relative_folder: "", authorization_source: "", available: false });
  if (notebook) return <ResearchNotebook initialEntryId={notebook} onClose={() => { setNotebook(null); void refresh(); }} onJump={c => { setNotebook(null); open(c.document_id, "right", c.physical_page, c); }} />;
  return <ModalDialog label="多文献研读空间" onClose={close}><div className="research-studio">
    <header className="studio-heading"><div><span className="eyebrow">CONNECTED READING</span><h2>多文献研读空间</h2></div><button disabled={busy} onClick={close}>关闭研读空间</button></header>
    <div className="studio-groupbar"><select aria-label="打开研读组" value={groupId} disabled={busy} onChange={e => loadGroup(e.target.value)}><option value="">临时研读 / 新建组</option>{groups.map(g => <option key={g.id} value={g.id}>{g.title} · {g.document_ids.length} 篇</option>)}</select><input aria-label="研读组名称" value={title} maxLength={120} placeholder="例如：相机陷阱方法比较" disabled={busy} onChange={e => setTitle(e.target.value)} /><button disabled={busy || !ids.length || !title.trim()} onClick={() => void saveGroup()}>保存研读组</button>{group && <><button disabled={busy} onClick={() => void saveGroup(true)}>另存为新组</button><button disabled={busy} onClick={() => void removeGroup()}>删除组</button></>}<button className="button button--primary" disabled={busy || !ids.length || selectedDocuments.length !== ids.length} onClick={ask}>向本组提问 →</button></div>
    <div className="studio-members"><span>{ids.length} / 8 篇</span>{ids.map(id => <button key={id} disabled={busy} title="从研读组移除" onClick={() => removeDocument(id)}>{documents.find(d => d.document_id === id)?.filename ?? "已不可用的文献"} ×</button>)}{!ids.length && <span>在下方选择论文，或从图谱中打开原文。</span>}</div>
    <nav className="studio-tabs" aria-label="联读功能"><button aria-pressed={mode === "reading"} onClick={() => switchMode("reading")}>双栏对照</button><button aria-pressed={mode === "ai"} onClick={() => switchMode("ai")}>AI 关系审核</button><button aria-pressed={mode === "questions"} onClick={() => { setQuestionFocus(""); switchMode("questions"); }}>问题与分歧</button><button aria-pressed={mode === "comparison"} onClick={() => { setComparisonOpened(true); switchMode("comparison"); }}>结构化对比</button><button aria-pressed={mode === "matrix"} onClick={() => switchMode("matrix")}>证据矩阵</button><button aria-pressed={mode === "graph"} onClick={() => switchMode("graph")}>知识图谱</button><button disabled={loading || busy} onClick={() => void refresh()}>刷新关联</button>{loading && <span role="status">正在读取关联…</span>}</nav>
    {error && <p className="studio-alert" role="alert">{error}</p>}{notice && <p className="studio-notice" role="status">{notice}</p>}
    <div className="studio-body">
    {mode === "reading" && <><div className="studio-reading-tools"><form onSubmit={e => { e.preventDefault(); if (search.trim()) setSearchRequest({ query: search.trim(), nonce: Date.now() }); }}><input aria-label="跨篇查找关键词" value={search} placeholder="两篇同时查找，例如：sampling effort" onChange={e => setSearch(e.target.value)} /><button disabled={!search.trim()} type="submit">两栏查找</button></form><button disabled={!left || !right} onClick={() => { const from = activeSide === "left" ? left : right; const to = activeSide === "left" ? right : left; const side = activeSide === "left" ? "right" : "left"; if (from && to) setPanes(current => ({ ...current, [side]: { id: to.id, page: alignedPage(from.page, from.total, to.total), nonce: (current[side]?.nonce ?? 0) + 1 } })); }}>按{activeSide === "left" ? "左" : "右"}栏阅读比例对齐</button><small>比例对齐仅同步位置，不代表章节对应。</small></div>
      <div className="studio-readers">{(["left", "right"] as const).map(side => <section key={side} className={`studio-reader-column${activeSide === side ? " studio-reader-column--active" : ""}`} onPointerDown={() => setActiveSide(side)} onFocusCapture={() => setActiveSide(side)}><header><span>{side === "left" ? "左栏" : "右栏"}</span><select aria-label={side === "left" ? "左栏文献" : "右栏文献"} value={panes[side]?.id ?? ""} disabled={busy} onChange={e => { if (e.target.value) open(e.target.value, side); }}><option value="">选择论文…</option>{documents.map(d => <option key={d.document_id} value={d.document_id} disabled={!ids.includes(d.document_id) && ids.length >= 8}>{d.filename}</option>)}</select>{onSolo && <button disabled={!panes[side] || busy} onClick={() => { if (panes[side] && canLeave()) onSolo(panes[side]!.id, positions[side]?.page ?? panes[side]!.page); }}>精读 / 批注</button>}</header><LinkedPdfPane side={side} location={panes[side]} searchRequest={searchRequest} onProgress={report} onReady={ready} /></section>)}</div>
      <details className="studio-manual-relation"><summary>手动记录文献关系{relationDirty ? " · 有未保存内容" : ""}</summary><form className="studio-relation studio-relation--question" onSubmit={e => { e.preventDefault(); void saveRelation(); }}><div><strong>记录两处原文的关系</strong><small>左侧第 {left?.page ?? "—"} 页 → 右侧第 {right?.page ?? "—"} 页 · 手动判断</small></div><label>研究问题<input aria-label="关系所属研究问题" list="relation-question-options" maxLength={500} value={relationQuestion} disabled={busy} placeholder="例如：采样强度是否影响密度估计？" onChange={e => setRelationQuestion(e.target.value)} /><datalist id="relation-question-options">{[...new Set([...questions, ...graph.edges.map(e => e.question ?? "")].filter(Boolean))].map(q => <option key={q} value={q} />)}</datalist></label><select aria-label="文献关系类型" value={relation} disabled={busy} onChange={e => setRelation(e.target.value)}><option value="related">相关研究</option><option value="supports">左侧支持右侧</option><option value="contradicts">存在分歧</option><option value="method">方法对照</option></select><select aria-label="差异主要来源" value={differenceBasis} disabled={busy} onChange={e => setDifferenceBasis(e.target.value)}>{Object.entries(DIFFERENCE_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select><textarea aria-label="方法样本与条件对照" maxLength={4000} rows={2} value={comparisonNote} disabled={busy} placeholder="左侧与右侧分别采用什么方法、样本与条件？哪些不同尚不能排除？" onChange={e => setComparisonNote(e.target.value)} /><textarea aria-label="关系依据与适用条件" rows={2} maxLength={4000} value={relationNote} disabled={busy} placeholder="写明支持或分歧的具体结论、条件和待核实之处…" onChange={e => setRelationNote(e.target.value)} /><button disabled={busy || !left || !right || !leftMeta || !rightMeta || left.id === right.id || !relationNote.trim() || !relationQuestion.trim()} type="submit">保存关系</button></form></details>
    </>}
    <div hidden={mode !== "ai"}><RelationReview initialAnalysisId={initialAnalysisId} ids={ids} documents={documents} onPair={pair} onBusy={setBusy} onDirty={setAnalysisDirty} onGraphChanged={refreshSavedGraph} /></div>
    {comparisonOpened && <div hidden={mode !== "comparison"}><StructuredComparison ids={ids} onBusy={setBusy} onEvidence={citation => open(citation.document_id, "right", citation.physical_page, citation)} /></div>}
    {mode === "questions" && <QuestionEvidence key={questionFocus} data={graph} scope={ids} initialQuestion={questionFocus} onPair={pair} onGraph={question => { setQuestionFocus(question); switchMode("graph"); }} onDelete={id => void deleteRelation(id)} onRead={() => switchMode("reading")} />}
    {mode === "matrix" && <EvidenceMatrix documents={evidenceDocuments} refreshKey={evidenceRefresh} onOpen={open} onNotebook={setNotebook} onEvidence={(c, side) => open(c.document_id, side, c.physical_page, c)} onAsk={question => { if (!busy && canLeave()) onAsk(ids, question); }} />}
    {mode === "graph" && <><label className="studio-scope"><input type="checkbox" checked={scopeAll} onChange={e => setScopeAll(e.target.checked)} /> 查看整个文献库（取消后仅看本组）</label>{!scopeAll && !ids.length ? <p className="studio-empty">当前研读组没有文献，请加入文献或查看整个文献库。</p> : <KnowledgeGraph key={questionFocus} initialFocus={graph.nodes.find(n => n.question_text && questionKey(n.question_text) === questionKey(questionFocus))?.id} onQuestion={question => { setQuestionFocus(question); switchMode("questions"); }} data={graph} scope={scopeAll ? [] : ids} onOpen={open} onEvidence={(c, side) => open(c.document_id, side, c.physical_page, c)} onPair={pair} onNotebook={setNotebook} onGroup={loadGroup} onDeleteRelation={id => void deleteRelation(id)} />}</>}
    </div>
  </div></ModalDialog>;
}
