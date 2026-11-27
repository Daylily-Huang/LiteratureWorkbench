import { useEffect, useRef, useState } from "react";
import { StudyCoverage, parseFullTextCoverage, type FullTextCoverage } from "./fullTextStudy";
import { workspaceRequest } from "./workspaceApi";

export type AnalysisReceipt = { request_id: string; status: "running" | "completed" | "failed" | "unknown"; id?: string; candidate_count?: number; cache_hit?: boolean; readonly?: boolean; error_code?: string; source_changed?: boolean };
type Attempt = { request_id: string; question: string; document_ids: string[]; force_refresh: boolean; created_at: string; reading_mode?: "retrieval" | "full_text"; source_versions?: Record<string, string>; plan_id?: string };
const PREFIX = "lw-relation-attempt-v1:";
function read(workspace: string, ids: string[], mode: string): Attempt[] {
  const items: Attempt[] = [];
  try {
    for (const key of Object.keys(localStorage).filter(key => key.startsWith(`${PREFIX}${workspace}:`))) {
      try {
        const value = JSON.parse(localStorage.getItem(key) ?? "null") as Attempt;
        if (value && /^[a-f0-9-]{36}$/i.test(value.request_id) && typeof value.question === "string" && value.question.length <= 500 && Array.isArray(value.document_ids) && value.document_ids.length <= 8 && value.document_ids.every(id => typeof id === "string") && JSON.stringify(value.document_ids) === JSON.stringify(ids) && typeof value.force_refresh === "boolean" && typeof value.created_at === "string" && (value.reading_mode ?? "retrieval") === mode) items.push(value);
      } catch { /* A broken browser record must not trigger a paid request. */ }
    }
  } catch { /* Read-only server recovery remains available by request ID. */ }
  return items.sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 20);
}

export function RelationRequestForm({ ids, workspace, question, onQuestion, disabled, onBusy, onComplete, canSubmit = () => true, initialReadingMode = "full_text" }: {
  ids: string[]; workspace: string; question: string; onQuestion(value: string): void; disabled: boolean;
  onBusy(value: boolean): void; onComplete(receipt: AnalysisReceipt, question: string): Promise<void>;
  canSubmit?(): boolean; initialReadingMode?: "retrieval" | "full_text";
}) {
  const [readingMode, setReadingMode] = useState(initialReadingMode);
  const [prepared, setPrepared] = useState<{ key: string; study: FullTextCoverage }>();
  const [preparing, setPreparing] = useState(false);
  const preparation = useRef<AbortController | null>(null);
  const ordered = JSON.stringify(ids);
  const context = `${workspace}/${ordered}/${readingMode}`;
  const [recoveredContext, setRecoveredContext] = useState("");
  const contextReady = recoveredContext === context;
  const owner = useRef(context); owner.current = context;
  const mounted = useRef(true);
  const active = useRef("");
  const request = useRef<AbortController | null>(null);
  const query = useRef<AbortController | null>(null);
  const callbacks = useRef({ onBusy, onComplete, onQuestion }); callbacks.current = { onBusy, onComplete, onQuestion };
  const questionRef = useRef(question); questionRef.current = question;
  const [attempt, setAttempt] = useState<Attempt>();
  const [receipt, setReceipt] = useState<AnalysisReceipt>();
  const [recoverable, setRecoverable] = useState<Attempt[]>([]);
  const [force, setForce] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [storageError, setStorageError] = useState(false);
  const preparationKey = JSON.stringify([context, question.trim(), force]);
  const ready = prepared?.key === preparationKey ? prepared.study : undefined;
  const mayCheck = !force && attempt?.question === question.trim();
  const owns = (key: string, id: string) => mounted.current && owner.current === key && active.current === id;
  const storageKey = (id: string) => `${PREFIX}${workspace}:${id}`;

  async function prepare() {
    if (disabled || waiting || !contextReady || preparation.current || !question.trim()) return;
    const controller = new AbortController(); preparation.current = controller; setPreparing(true); setError("");
    const key = preparationKey; const ownerKey = context;
    try {
      const value = parseFullTextCoverage(await workspaceRequest("analyses/prepare", { question: question.trim(), document_ids: ids, force_refresh: force }, "POST", controller.signal));
      if (!value || !value.plan_id || JSON.stringify(value.coverage.map(d => d.document_id)) !== ordered) throw new Error("invalid_coverage");
      if (mounted.current && owner.current === ownerKey) {
        setPrepared({ key, study: value });
        if (receipt?.error_code === "full_text_plan_changed") { setAttempt(undefined); setReceipt(undefined); active.current = ""; }
      }
    } catch (e) { if (mounted.current && owner.current === ownerKey && !controller.signal.aborted) setError(e instanceof Error && e.message === "full_text_online_required" ? "全文关系分析需要在线模型，未调用 API。也可切换片段模式使用本地模型。" : "全文准备未通过，没有调用模型。请确认各篇解析完整、没有空白页，且总请求未超出大小限制，再重试。"); }
    finally { if (preparation.current === controller) { preparation.current = null; if (mounted.current) setPreparing(false); } }
  }
  async function apply(value: AnalysisReceipt, saved: Attempt, key: string) {
    if (!owns(key, saved.request_id)) return;
    setReceipt(value);
    if (value.status === "completed" && value.id) {
      try {
        await callbacks.current.onComplete(value, saved.question);
        if (!owns(key, saved.request_id)) return;
        try { localStorage.removeItem(storageKey(saved.request_id)); } catch { /* The saved result is already in the database. */ }
        setRecoverable(items => items.filter(item => item.request_id !== saved.request_id));
      } catch { if (owns(key, saved.request_id)) setError("分析已保存，但列表尚未读取成功。请查询原请求，无需重新分析。"); }
    }
  }
  async function check(saved: Attempt, key = context) {
    if (query.current || !owns(key, saved.request_id)) return;
    const controller = new AbortController(); query.current = controller; setChecking(true); setError("");
    try { await apply(await workspaceRequest<AnalysisReceipt>(`analyses/requests/${saved.request_id}`, undefined, "GET", controller.signal), saved, key); }
    catch { if (owns(key, saved.request_id) && !controller.signal.aborted) { setReceipt({ request_id: saved.request_id, status: "unknown" }); setError("暂时无法确认原请求结果。请稍后查询；不会自动重发分析。"); } }
    finally { if (query.current === controller) { query.current = null; if (mounted.current) setChecking(false); } }
  }
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; preparation.current?.abort(); request.current?.abort(); query.current?.abort(); if (request.current) callbacks.current.onBusy(false); };
  }, []);
  useEffect(() => {
    preparation.current?.abort(); preparation.current = null; setPreparing(false); setPrepared(undefined);
    request.current?.abort(); query.current?.abort();
    if (request.current) callbacks.current.onBusy(false);
    request.current = null; query.current = null; active.current = "";
    setWaiting(false); setChecking(false); setReceipt(undefined); setAttempt(undefined); setForce(false); setError("");
    const items = workspace ? read(workspace, JSON.parse(ordered) as string[], readingMode) : [];
    setRecoverable(items);
    // A render with a new workspace/mode must not accept a click before this
    // recovery effect has reset old request ownership.
    setRecoveredContext(context);
    const latest = items[0];
    if (latest) {
      active.current = latest.request_id; setAttempt(latest);
      if (!questionRef.current) callbacks.current.onQuestion(latest.question);
      void check(latest, context);
    }
    return () => { query.current?.abort(); query.current = null; };
  }, [context, workspace, ordered]);
  useEffect(() => {
    if (!attempt || receipt?.status !== "running") return;
    let ticks = 0;
    const timer = window.setInterval(() => { if (++ticks <= 120) void check(attempt); else window.clearInterval(timer); }, 3000);
    return () => window.clearInterval(timer);
  }, [attempt?.request_id, receipt?.status, context]);

  async function submit() {
    if (disabled || waiting || preparing || !contextReady || request.current || !workspace || ids.length < 2 || !question.trim()) return;
    if (!canSubmit()) return;
    if (!force && attempt?.question === question.trim()) { await check(attempt); return; }
    if (readingMode === "full_text" && !ready) return;
    const saved: Attempt = { request_id: crypto.randomUUID(), question: question.trim(), document_ids: [...ids], force_refresh: force, created_at: new Date().toISOString(), ...(readingMode === "full_text" ? { reading_mode: readingMode, source_versions: ready!.source_versions, plan_id: ready!.plan_id } : {}) };
    const key = context;
    query.current?.abort(); query.current = null; active.current = saved.request_id;
    setAttempt(saved); setReceipt(undefined); setError(""); setForce(false);
    try { localStorage.setItem(storageKey(saved.request_id), JSON.stringify(saved)); setStorageError(false); }
    catch { setStorageError(true); }
    setRecoverable(items => [saved, ...items].slice(0, 20));
    const controller = new AbortController(); request.current = controller;
    setWaiting(true); callbacks.current.onBusy(true);
    try {
      const value = await workspaceRequest<AnalysisReceipt>("analyses", { question: saved.question, document_ids: saved.document_ids, request_id: saved.request_id, force_refresh: saved.force_refresh, ...(saved.reading_mode === "full_text" ? { reading_mode: saved.reading_mode, source_versions: saved.source_versions, plan_id: saved.plan_id } : {}) }, "POST", controller.signal);
      if (!controller.signal.aborted) await apply(value, saved, key);
    } catch { if (owns(key, saved.request_id)) await check(saved, key); }
    finally {
      if (request.current === controller) { request.current = null; if (mounted.current) { setWaiting(false); callbacks.current.onBusy(false); } }
    }
  }
  function stopWaiting() {
    request.current?.abort(); request.current = null; setWaiting(false); callbacks.current.onBusy(false);
    if (attempt) { setReceipt({ request_id: attempt.request_id, status: "running" }); void check(attempt); }
  }
  return <div className="relation-request-form">
    <label>关系分析阅读方式<select aria-label="关系分析阅读方式" value={readingMode} disabled={disabled || waiting || preparing} onChange={e => setReadingMode(e.target.value as typeof readingMode)}><option value="full_text">全文研读 / 增量复用（推荐）</option><option value="retrieval">检索片段对比</option></select></label>
    {readingMode === "full_text" && <div><button type="button" disabled={disabled || waiting || preparing || !contextReady || !workspace || ids.length < 2 || !question.trim()} onClick={() => void prepare()}>{preparing ? "正在检查全文…" : "免费检查全文与复用范围"}</button>{ready && <StudyCoverage study={ready} />}<small>准备检查不调用模型；提交后最多发起一次分析请求，相同关系结果命中缓存则不新增调用。</small></div>}

    <form className="review-generate" onSubmit={event => { event.preventDefault(); void submit(); }}><label>本次研究问题<input aria-label="AI 关系分析问题" maxLength={500} value={question} disabled={disabled || waiting || preparing} placeholder="例如：采样设计能否解释两篇研究的估计差异？" onChange={event => onQuestion(event.target.value)} /></label><button disabled={disabled || waiting || preparing || !contextReady || !workspace || ids.length < 2 || !question.trim() || (readingMode === "full_text" && !ready && !mayCheck)} type="submit">分析所选文献关系</button>{waiting && <button type="button" onClick={stopWaiting}>停止等待</button>}</form>
    <label className="assistant-panel__hint"><input type="checkbox" checked={force} disabled={disabled || waiting || preparing} onChange={event => setForce(event.target.checked)} />跳过已有结果，明确发起新分析（可能计费）</label>
    <p className="assistant-panel__hint">{readingMode === "full_text" ? "按上方清单读取全文或复用旧分析与核验引句，不含图片视觉理解。" : "本模式仅比较检索片段。"}相同问题、阅读方式、原文/解析版本与模型设置优先复用结果。新分析可能调用 API；停止等待不会取消已发出的模型请求。</p>
    {error && !attempt && <p role="alert">{error}</p>}
    {storageError && <p role="alert">浏览器未能保存恢复信息；服务端仍会防止重复提交，请保留本页并记录请求编号。</p>}
    {attempt && <div className="relation-request-status"><small>请求编号：{attempt.request_id}</small><p role="status">{waiting ? "正在等待分析结果…" : receipt?.status === "completed" ? (receipt.source_changed ? "已找到历史结果，原文版本已变化；仅供回看。" : receipt.readonly ? "已找回保存的分析；本次查询未调用 AI。" : receipt.cache_hit ? "复用已有分析；本次未新增 AI 请求。" : "本次分析已保存，可能已产生 API 费用。") : receipt?.status === "running" ? "原请求已登记，尚未确认完成。可关闭后回来查询，请勿重复发起。" : receipt?.status === "failed" ? `原请求未完成（${receipt.error_code || "结果未就绪"}），不会自动重发；如需新分析，请勾选上方选项。` : "原请求状态不明，只查询不会重新调用 AI。重启后也不会自动重发。"}</p><button disabled={checking || waiting} onClick={() => void check(attempt)}>{checking ? "正在查询…" : "查询原请求 / 查看结果"}</button>{error && <p role="alert">{error}</p>}{(receipt?.status === "unknown" || receipt?.status === "failed") && <small>可能已请求模型，不能据此判断没有计费。确认要重试时请明确发起新分析。</small>}</div>}
    {recoverable.length > 1 && <details><summary>可恢复的分析请求 · {recoverable.length}</summary><ul>{recoverable.map(item => <li key={item.request_id}><button disabled={waiting || disabled} onClick={() => { query.current?.abort(); query.current = null; active.current = item.request_id; setAttempt(item); callbacks.current.onQuestion(item.question); void check(item); }}>{item.question} · {new Date(item.created_at).toLocaleString()}</button></li>)}</ul></details>}
  </div>;
}
