import { useEffect, useRef, useState } from "react";
import type { DocumentListItem } from "../library/documents";
import { downloadText } from "../ui/exportText";
import type { LibraryCitation } from "./AssistantPanel";
import { DIFFERENCE_LABELS, type GraphEdge } from "./graphModel";
import { StudyCoverage, parseFullTextCoverage, type FullTextCoverage } from "./fullTextStudy";
import { RelationRequestForm, type AnalysisReceipt } from "./RelationRequestForm";
import { workspaceRequest } from "./workspaceApi";

export type StudyOrigin = { cache_key: string; claim_index: number; conclusion: string; evidence?: LibraryCitation[]; documents: { document_id: string; display_name: string; read_mode: string }[] };
export type RelationProposal = { study_origin?: StudyOrigin; source: LibraryCitation; target: LibraryCitation; relation: string; difference_basis: string; rationale: string; comparison_note: string; caveats: string[] };
type Review = { relation: string; difference_basis: string; note: string; comparison_note: string };
export type RelationCandidate = { id: string; proposal: RelationProposal; status: "pending" | "accepted" | "rejected"; revision: number; relation_id: string | null; source_changed: boolean; review: Review | null };
export type RelationAnalysis = { reading_coverage?: FullTextCoverage | null; id: string; question: string; document_ids: string[]; provider: string; model: string; fallback_reason: string | null; summary: string; warnings: string[]; created_at: string; coverage: { document_id: string; display_name: string; retrieved: boolean; has_candidate: boolean; reading_mode?: string }[]; candidates: RelationCandidate[] };
const LABELS: Record<string, string> = { supports: "左侧支持右侧", contradicts: "存在矛盾 / 分歧", method: "方法对照", related: "相关研究" };
const message = (error: unknown) => ({ source_changed: "原文已变化或不可用，不能确认这条旧候选。请重新分析当前版本。", page_unavailable: "原文页码尚未确认，请完成解析后重试。", candidate_conflict: "这条候选已在其他窗口处理，请刷新审核记录。", invalid_relation_analysis: "AI 返回的分析格式不合格，没有生成可审核候选。请重试或调整问题。", deepseek_key_unavailable: "当前 AI 服务未配置密钥，请在设置中配置或选择本地模型。", document_not_found: "所选文献已不可用，请重新选择。", qwen_failed: "本地模型未能完成分析，请检查模型状态后重试。" } as Record<string, string>)[error instanceof Error ? error.message : ""] ?? "分析或审核未完成。请检查模型设置或稍后重试，当前草稿仍保留。";

function CandidateCard({ candidate, question, busy, onDecision, onPair, onDirty }: { candidate: RelationCandidate; question: string; busy: boolean; onDecision(id: string, action: "accept" | "reject", body: object): Promise<void>; onPair(edge: GraphEdge): void; onDirty(id: string, dirty: boolean): void }) {
  const p = candidate.proposal;
  const [draft, setDraft] = useState<Review>({ relation: p.relation, difference_basis: p.difference_basis, note: p.rationale, comparison_note: p.comparison_note });
  const [confirmed, setConfirmed] = useState(false);
  const dirty = candidate.status === "pending" && (confirmed || draft.relation !== p.relation || draft.difference_basis !== p.difference_basis || draft.note !== p.rationale || draft.comparison_note !== p.comparison_note);
  useEffect(() => { onDirty(candidate.id, dirty); return () => onDirty(candidate.id, false); }, [candidate.id, dirty, onDirty]);
  function change(value: Partial<Review>) { setDraft(current => ({ ...current, ...value })); setConfirmed(false); }
  return <article className="review-card">
    <header><strong>{p.study_origin ? "精读结论转入 · 关系待判断" : `${LABELS[p.relation]} · AI 建议`}</strong><span>{candidate.status === "pending" ? "待你审核 · 尚未加入图谱" : candidate.status === "rejected" ? "已拒绝 · 未加入图谱" : candidate.relation_id ? "已确认加入图谱" : "已确认，图谱关系后来已删除"}</span></header>
    <div className="review-quotes">{(["source", "target"] as const).map((side, i) => <section key={side}><h4>{i === 0 ? "左侧" : "右侧"} · {p[side].display_name} · 第 {p[side].physical_page} 页</h4><blockquote>{p[side].excerpt}</blockquote><small>引句已与转入时的原文匹配，关系推理仍需核对。</small></section>)}</div>
    <button disabled={busy} onClick={() => { if (!candidate.source_changed || window.confirm("来源已变化，仍然打开当前文件的对应页码吗？")) onPair({ id: candidate.id, source: "doc:" + p.source.document_id, target: "doc:" + p.target.document_id, kind: "manual", label: LABELS[p.relation] ?? "候选关系", source_page: p.source.physical_page, target_page: p.target.physical_page, source_changed: candidate.source_changed, source_content_id: p.source.content_id, target_content_id: p.target.content_id, evidence: { source: p.source, target: p.target } }); }}>核对两侧原文</button>
    {candidate.source_changed && <p className="graph-warning">来源已变化或不可用。保留旧摘录供查阅，禁止直接确认旧候选。</p>}
    {p.study_origin && <p className="study-cache-label">来自全文研读缓存 · 转入未调用 API · 原结论属于 {p.study_origin.documents.length} 篇研读组：{p.study_origin.documents.map((d, i) => `第 ${i + 1} 篇 ${d.display_name}`).join("；")}。共同引用不自动构成支持或矛盾。</p>}
    {p.study_origin?.evidence && <details><summary>原精读结论的全部依据 · 保留多篇语境</summary>{p.study_origin.evidence.map((e, i) => <section key={i}><small>{e.display_name} · 第 {e.physical_page} 页</small><blockquote>{e.excerpt}</blockquote></section>)}</details>}
    <details><summary>查看 AI 原始解释与待核实点</summary><p>{p.rationale}</p><p>{p.comparison_note}</p><ul>{p.caveats.map((c, i) => <li key={i}>{c}</li>)}</ul></details>
    {candidate.status === "pending" ? <><div className="review-fields"><label>拟加入的关系<select aria-label="审核关系类型" value={draft.relation} disabled={busy} onChange={e => change({ relation: e.target.value })}>{Object.entries(LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label><label>差异主要来源<select aria-label="审核差异来源" value={draft.difference_basis} disabled={busy} onChange={e => change({ difference_basis: e.target.value })}>{Object.entries(DIFFERENCE_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label><label>关系依据（可修订）<textarea aria-label="审核关系依据" value={draft.note} maxLength={4000} disabled={busy} rows={3} onChange={e => change({ note: e.target.value })} /></label><label>方法、样本与条件对照（可修订）<textarea aria-label="审核对照说明" value={draft.comparison_note} maxLength={4000} disabled={busy} rows={3} onChange={e => change({ comparison_note: e.target.value })} /></label></div>
      <label className="review-confirm"><input type="checkbox" checked={confirmed} disabled={busy || candidate.source_changed} onChange={e => setConfirmed(e.target.checked)} />我已核对两侧原文，同意将当前修订后的关系加入图谱</label><footer><button className="button button--primary" disabled={busy || !confirmed || candidate.source_changed || !draft.note.trim() || !draft.comparison_note.trim()} onClick={() => void onDecision(candidate.id, "accept", { ...draft, confirmed: true, revision: candidate.revision })}>确认并加入图谱</button><button disabled={busy} onClick={() => { if (window.confirm("拒绝这条候选？记录仍可回看，原文和图谱不会被修改。")) void onDecision(candidate.id, "reject", { revision: candidate.revision }); }}>拒绝此建议</button></footer></>
      : candidate.review && <div><p>确认后的关系：{LABELS[candidate.review.relation]} · {DIFFERENCE_LABELS[candidate.review.difference_basis]}</p><p>{candidate.review.note}</p><p>{candidate.review.comparison_note}</p></div>}
    <small>研究问题：{question} · 用户确认不等于科学结论已验证。</small>
  </article>;
}

export function RelationReview({ ids, documents, initialAnalysisId, onPair, onBusy, onDirty, onGraphChanged }: { ids: string[]; documents: DocumentListItem[]; initialAnalysisId?: string; onPair(edge: GraphEdge): void; onBusy(value: boolean): void; onDirty(value: boolean): void; onGraphChanged(): Promise<void> }) {
  const [workspace, setWorkspace] = useState("");
  const scope = JSON.stringify(ids);
  const scopeOwner = useRef(scope); scopeOwner.current = scope;
  const mounted = useRef(true); useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [question, setQuestion] = useState(""); const [savedQuestion, setSavedQuestion] = useState("");
  const [items, setItems] = useState<RelationAnalysis[]>([]); const [active, setActive] = useState(initialAnalysisId ?? "");
  const [status, setStatus] = useState(initialAnalysisId ? "all" : "pending"); const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false); const [loading, setLoading] = useState(false); const [truncated, setTruncated] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, boolean>>({});
  const draftsRef = useRef(drafts); draftsRef.current = drafts;
  const activeRef = useRef(active); activeRef.current = active;
  const updateDirty = useRef((id: string, value: boolean) => setDrafts(current => current[id] === value ? current : { ...current, [id]: value })).current;
  const dirty = question !== savedQuestion || Object.values(drafts).some(Boolean);
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  const scoped = items.filter(a => !ids.length || a.document_ids.every(id => ids.includes(id)));
  const current = scoped.find(a => a.id === active) ?? scoped[0];
  async function load(preferred?: string) {
    const owner = scopeOwner.current;
    const value = await workspaceRequest<{ items: RelationAnalysis[]; truncated: boolean; workspace_id?: string }>("analyses");
    preferred = preferred ?? activeRef.current;
    if (preferred && !value.items.some(item => item.id === preferred)) {
      const detail = await workspaceRequest<{ items: RelationAnalysis[] }>(`analyses/${preferred}`);
      value.items = [...detail.items, ...value.items];
      if (!detail.items.length) throw new Error("analysis_unavailable");
    }
    if (!mounted.current || scopeOwner.current !== owner) return;
    setActive(current => current || value.items.find(item => !ids.length || item.document_ids.every(id => ids.includes(id)))?.id || "");
    setItems(value.items); setTruncated(value.truncated); setWorkspace(value.workspace_id ?? "");
  }
  async function refresh() {
    const owner = scopeOwner.current;
    setLoading(true); setError("");
    try { await load(); }
    catch (e) { if (mounted.current && scopeOwner.current === owner) setError(message(e)); }
    finally { if (mounted.current && scopeOwner.current === owner) setLoading(false); }
  }
  useEffect(() => { void refresh(); }, [scope]);
  async function completed(result: AnalysisReceipt, saved: string) {
    const owner = scopeOwner.current;
    await load(result.id);
    if (!mounted.current || scopeOwner.current !== owner) return;
    setSavedQuestion(saved);
    if (Object.values(draftsRef.current).some(Boolean)) { setNotice("分析已保存，当前审核草稿仍保留。可在分析记录中选择新记录查看。"); return; }
    setActive(result.id ?? ""); setStatus("pending");
    setNotice(result.cache_hit || result.readonly ? "已打开保存的分析；原有审核状态保留，未自动新增图谱关系。" : `分析已保存：${result.candidate_count ?? 0} 条可审核建议。尚未加入图谱。`);
  }
  async function decide(id: string, action: "accept" | "reject", body: object) {
    if (busy) return;
    setBusy(true); onBusy(true); setError(""); let persisted = false;
    try { const result = await workspaceRequest<{ already_accepted?: boolean }>(`candidates/${id}/${action}`, body); persisted = true; setNotice(action === "reject" ? "已拒绝，建议不会加入图谱。" : result.already_accepted ? "此候选此前已确认，本次没有重复创建或覆盖关系。" : "已按你的确认加入图谱，原始 AI 建议与修订均已留存。"); await load(); await onGraphChanged(); }
    catch (e) { setError(persisted ? "审核已保存，但视图刷新失败。请刷新记录，避免重复操作。" : message(e)); }
    finally { setBusy(false); onBusy(false); }
  }
  return <section className="relation-review" aria-label="AI 关系审核">
    <h3>AI 分析，多篇核对，逐条确认</h3><p>沿用研究助手的模型与数据发送设置，仅分析当前选择的 2–8 篇。AI 建议先保存在这里，确认前不会进入图谱。</p>
    <p>当前选择：{ids.map(id => documents.find(d => d.document_id === id)?.filename ?? "不可用文献").join("、") || "请先选择至少两篇文献"}</p>
    <RelationRequestForm ids={ids} workspace={workspace} question={question} onQuestion={setQuestion} disabled={busy || ids.some(id => !documents.some(d => d.document_id === id))} onBusy={value => { setBusy(value); onBusy(value); }} onComplete={completed} canSubmit={() => !Object.values(drafts).some(Boolean) || window.confirm("切换分析前，请确认放弃当前未提交的审核修订。继续吗？")} />
    {busy && <p role="status">正在处理，请稍候。生成建议不会自动加入图谱。</p>}{notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
    <div className="review-history"><label>分析记录<select aria-label="选择分析记录" value={current?.id ?? ""} disabled={busy} onChange={e => { if (!Object.values(drafts).some(Boolean) || window.confirm("切换后将放弃当前未提交的审核修订，继续吗？")) setActive(e.target.value); }}><option value="">选择记录</option>{scoped.map(a => <option key={a.id} value={a.id}>{new Date(a.created_at).toLocaleString()} · {a.question} · {a.candidates.length} 条</option>)}</select></label><button disabled={busy || loading} onClick={() => void refresh()}>刷新候选记录</button><select aria-label="候选审核状态" value={status} disabled={busy} onChange={e => { if (!Object.values(drafts).some(Boolean) || window.confirm("切换后将放弃当前未提交的审核修订，继续吗？")) setStatus(e.target.value); }}><option value="pending">待审核</option><option value="accepted">已确认</option><option value="rejected">已拒绝</option><option value="all">全部状态</option></select>{current && <button onClick={() => downloadText("AI关系分析与审核记录.json", JSON.stringify(current, null, 2))}>导出分析记录</button>}</div>
    {truncated && <p>当前载入最近 100 次分析，可定期导出审核记录。</p>}
    {current ? <><h4>{current.question}</h4><p>{current.summary}</p><small>来源：{current.provider} / {current.model || "未调用模型"}{current.fallback_reason ? " · 已使用备用模型" : ""} · {current.reading_coverage ? "全文 / 增量关系分析" : current.provider === "study_cache" ? "已有全文研读依据 · 本次转入未调用 AI" : "仅限本次检索覆盖"}</small>{parseFullTextCoverage(current.reading_coverage) && <div><p>生成此记录时的阅读范围 · 本次查看未调用 AI；以下范围说明指当时分析。</p><StudyCoverage study={current.reading_coverage!} sent /></div>}<ul className="review-coverage">{current.coverage.map(c => <li key={c.document_id}>{c.display_name}：{current.reading_coverage ? (c.has_candidate ? "有双侧依据候选" : "已纳入本次提供范围，未形成合格候选") : c.reading_mode === "study_cache" ? (c.has_candidate ? "本次选中的双侧依据" : "原研读组语境，未作为本次双侧依据") : !c.retrieved ? "未检索到相关片段" : c.has_candidate ? "有双侧依据候选" : "已检索，未形成合格关系"}</li>)}</ul><ul>{current.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>{!current.candidates.some(c => status === "all" || c.status === status) && <p className="studio-empty">当前状态没有候选。没有合格候选不代表这些文献不存在关系。</p>}{current.candidates.filter(c => status === "all" || c.status === status).map(c => <CandidateCard key={c.id} candidate={c} question={current.question} busy={busy} onDecision={decide} onPair={onPair} onDirty={updateDirty} />)}</> : <p className="studio-empty">当前选择范围没有分析记录。填写问题后开始分析；也可清空研读组选择，查看全部历史。</p>}
  </section>;
}
