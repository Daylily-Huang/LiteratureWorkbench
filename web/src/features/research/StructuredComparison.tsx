import { useEffect, useRef, useState } from "react";
import type { LibraryCitation } from "./AssistantPanel";
import { StudyCoverage, type FullTextCoverage } from "./fullTextStudy";
import { workspaceRequest } from "./workspaceApi";
import { downloadText, markdownText } from "../ui/exportText";

type Preparation = FullTextCoverage & { estimated_upper_cny: string; pricing_basis: string; max_output_tokens: number };
type Cell = { status: "supported" | "insufficient" | "unverified"; value: string; citations: LibraryCitation[]; validation_issue?: string };
export type ComparisonResult = { question: string; model: string; created_at: string; fields: Record<string, string>; rows: { document_id: string; display_name: string; content_id: string; source_content_id_at_generation?: string; cells: Record<string, Cell> }[]; coverage: Preparation; cache_saved: boolean; source_versions: Record<string, string> };
type Receipt = { request_id: string; status: "running" | "completed" | "failed" | "unknown"; cache_hit: boolean; source_changed: boolean; result: ComparisonResult | null; error_code?: string };
type History = { request_id: string; document_ids: string[]; question: string; created_at: string };
const DEFAULT_QUESTION = "逐篇比较研究对象、样本量与采样设计、方法与指标、关键假设与适用条件、主要结果、作者指出的局限；保留数值单位和不同实验条件，不把方法差异当作结论矛盾。";
const REVIEW_FIELDS: Record<string, string> = {
  sample: "请核对样本量、统计对象和单位",
  method: "请核对方法、指标和单位",
  assumptions: "请核对假设与适用条件",
  results: "请核对数值、单位和结论范围",
  limitations: "请核对局限和建议的适用条件",
};
// A conservative hint, not semantic verification: only check a few common
// explicit number-unit pairs against the same cited text. Unrecognized forms
// still require the reader to inspect the complete original quotation.
const UNIT_PATTERNS: Array<[string, string]> = [
  ["样本", "samples?|specimens?|样本|样品|份"],
  ["个体", "individuals?|animals?|个体|只|头"],
  ["地点", "sites?|locations?|地点|站点|样点"],
  ["采样次数", "occasions?|visits?|次采样|次调查"],
  ["平方公里", "km(?:²|2|\\^2)|平方公里|平方千米"],
  ["公顷", "ha\\b|hectares?|公顷"],
  ["百分比", "%|percent|百分之"],
];
export function quantityWarnings(cell: Cell): string[] {
  const source = cell.citations.map(c => c.excerpt).join(" ");
  const warnings: string[] = [];
  for (const [label, unit] of UNIT_PATTERNS) {
    const pattern = new RegExp(`(\\d[\\d,]*(?:\\.\\d+)?)\\s*(?:个|份|只|头)?\\s*(?:${unit})`, "giu");
    for (const match of cell.value.matchAll(pattern)) {
      const number = match[1]!;
      const escaped = number.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const sourcePair = new RegExp(`(?<![\\d,.])${escaped}(?![\\d,.])\\s*(?:个|份|只|头)?\\s*(?:${unit})`, "iu");
      if (!sourcePair.test(source)) warnings.push(`${number} ${label}`);
    }
  }
  return [...new Set(warnings)];
}
const ERRORS: Record<string, string> = { full_text_plan_changed: "正文、设置或可复用范围已变化，请重新免费检查。", comparison_unverified: "返回的表格或原文依据未通过核对，没有保存为有效对比。请求可能已计费，不会自动重试。", full_text_unverified_answer: "一处原句未通过核对，没有保存为有效对比。请求可能已计费，不会自动重试。", full_text_online_required: "生成新表格需要在线模型；查看已有表格不调用模型。", full_text_too_large: "全文请求超出大小限制，请减少选篇；没有自动截断或调用模型。", comparison_pricing_unknown: "当前模型没有已核对的费用估算，请先选择已支持的DeepSeek模型。" };
function message(error: unknown) { return ERRORS[error instanceof Error ? error.message : ""] ?? "操作未完成。已有请求请先查询结果，不会自动重新调用 AI。"; }

export function comparisonMarkdown(result: ComparisonResult, changed: boolean): string {
  return [`# 结构化文献对比`, `问题：${markdownText(result.question)}`, `模型：${result.model} · 生成：${result.created_at}`, changed ? "历史来源已变化，以下为当时保存的依据。" : "原句匹配不代表AI归纳已科学验证。", ...result.rows.flatMap(row => [`\n## ${markdownText(row.display_name)}`, `生成时原文版本：${row.source_content_id_at_generation ?? row.content_id}`, `当前核对版本：${row.content_id}`, ...Object.entries(result.fields).map(([field, label]) => {
    const cell = row.cells[field];
    return `\n### ${label}\n${cell?.status === "supported" ? `${REVIEW_FIELDS[field] ?? "请核对归纳与原文"}；${quantityWarnings(cell).length ? `数字与单位未在引句相邻匹配：${quantityWarnings(cell).join("、")}；` : ""}以下为未经人工确认的 AI 草稿：\n${markdownText(cell.value)}` : cell?.status === "unverified" ? "原文依据未通过核对，此项未填入。" : "本次证据未覆盖，不代表原文未报告。"}\n${cell?.citations.map(c => `原文第 ${c.physical_page} 页（${c.content_id}）：${markdownText(c.excerpt)}`).join("\n") ?? ""}`;
  })])].join("\n");
}

export function StructuredComparison({ ids, onEvidence, onBusy }: { ids: string[]; onEvidence(citation: LibraryCitation): void; onBusy(value: boolean): void }) {
  const [question, setQuestion] = useState(DEFAULT_QUESTION);
  const [force, setForce] = useState(false);
  const [prepared, setPrepared] = useState<{ key: string; value: Preparation }>();
  const [receipt, setReceipt] = useState<Receipt>();
  const [attempt, setAttempt] = useState<{ id: string; question: string; force: boolean }>();
  const [history, setHistory] = useState<History[]>([]);
  const [workspace, setWorkspace] = useState("");
  const [loadedScope, setLoadedScope] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [storageError, setStorageError] = useState(false);
  const scope = JSON.stringify(ids);
  const context = useRef(scope); context.current = scope;
  const mounted = useRef(true);
  const request = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const currentQuestion = useRef(question); currentQuestion.current = question;
  const key = JSON.stringify([scope, question.trim(), force]);
  const ready = prepared?.key === key ? prepared.value : undefined;
  const contextReady = loadedScope === scope;
  const result = contextReady && receipt?.status === "completed" ? receipt.result : null;
  const callbacks = useRef({ onBusy }); callbacks.current = { onBusy };
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; request.current?.abort(); callbacks.current.onBusy(false); }; }, []);
  useEffect(() => {
    let active = true; const controller = new AbortController();
    setPrepared(undefined); setReceipt(undefined); setAttempt(undefined); setHistory([]); setWorkspace(""); setError("");
    workspaceRequest<{ workspace_id: string; items: History[] }>("comparisons", undefined, "GET", controller.signal).then(async value => {
      if (!active) return;
      setWorkspace(value.workspace_id); setLoadedScope(scope); const items = value.items.filter(item => JSON.stringify(item.document_ids) === scope); setHistory(items);
      let id = "";
      try { id = localStorage.getItem(`lw-comparison:${value.workspace_id}:${scope}`) ?? ""; } catch { setStorageError(true); }
      id ||= items[0]?.request_id ?? "";
      if (id) {
        const saved = await workspaceRequest<Receipt>(`comparisons/${id}`, undefined, "GET", controller.signal);
        if (active) { setReceipt(saved); setAttempt({ id, question: items.find(item => item.request_id === id)?.question ?? currentQuestion.current, force: false }); }
      }
    }).catch(() => { if (active) setError("历史对比暂未读取成功，没有调用模型。可关闭此页后重新进入。"); });
    return () => { active = false; controller.abort(); };
  }, [scope]);

  async function operate(action: "prepare" | "submit" | "query", savedId?: string) {
    if (busyRef.current || !workspace || !contextReady || (action !== "query" && (ids.length < 2 || !question.trim()))) return;
    if (action === "submit" && !ready) return;
    const controller = new AbortController(); request.current = controller; busyRef.current = true;
    const owner = scope; setBusy(true); onBusy(true); setError("");
    const owns = () => mounted.current && context.current === owner && !controller.signal.aborted;
    try {
      if (action === "prepare") {
        const value = await workspaceRequest<Preparation>("comparisons/prepare", { question: question.trim(), document_ids: ids, force_refresh: force }, "POST", controller.signal);
        if (JSON.stringify(value.coverage.map(d => d.document_id)) !== scope || !value.plan_id) throw new Error("invalid_scope");
        if (owns()) setPrepared({ key, value });
      } else {
        const querying = action === "query" || (attempt?.question === question.trim() && attempt.force === force);
        const id = savedId ?? (querying ? attempt?.id : crypto.randomUUID());
        if (!id) return;
        if (!querying) {
          try { localStorage.setItem(`lw-comparison:${workspace}:${scope}`, id); } catch { setStorageError(true); }
          setAttempt({ id, question: question.trim(), force });
        }
        const value = await workspaceRequest<Receipt>(querying ? `comparisons/${id}` : "comparisons",
          querying ? undefined : { request_id: id, question: question.trim(), document_ids: ids,
            force_refresh: force, source_versions: ready!.source_versions, plan_id: ready!.plan_id }, querying ? "GET" : "POST", controller.signal);
        if (owns()) { setReceipt(value); if (savedId) setAttempt({ id, question: value.result?.question ?? question, force }); setPrepared(undefined); }
      }
    } catch (e) { if (owns()) setError(message(e)); }
    finally { if (request.current === controller) { request.current = null; busyRef.current = false; if (mounted.current) { setBusy(false); callbacks.current.onBusy(false); } } }
  }
  return <section className="structured-comparison" aria-label="结构化对比表">
    <h3>逐篇结构化对比</h3><p>每个已填项保留原文依据；“证据未覆盖”不等于原论文没有报告。引句匹配不能验证样本单位、数值指代或适用条件，请结合原文核对 AI 草稿。</p>
    <label>本次比较问题<textarea aria-label="结构化比较问题" maxLength={500} value={question} disabled={busy} onChange={e => setQuestion(e.target.value)} /></label>
    <label><input type="checkbox" checked={force} disabled={busy} onChange={e => setForce(e.target.checked)} /> 跳过表格缓存与旧分析，重新读全文（会调用API；失败的上次请求也可能已计费）</label>
    <div className="comparison-actions"><button disabled={busy || !contextReady || !workspace || ids.length < 2 || !question.trim()} onClick={() => void operate("prepare")}>免费检查范围与费用</button><button disabled={busy || !contextReady || !ready || !workspace} onClick={() => void operate("submit")}>{ready?.cache_status === "hit" ? "读取缓存对比表" : "生成结构化对比表（API）"}</button>{attempt && <><button disabled={busy} onClick={() => void operate("query")}>查询已有请求（免费）</button><button disabled={busy} onClick={() => { if (window.confirm("已有请求可能已计费。确定另建一次请求并跳过缓存重读吗？")) { setAttempt(undefined); setReceipt(undefined); setPrepared(undefined); setForce(true); } }}>另建请求（可能重复计费）</button></>}{busy && <button onClick={() => { request.current?.abort(); request.current = null; busyRef.current = false; setBusy(false); onBusy(false); }}>停止等待</button>}</div>
    {ids.length < 2 && <p>请至少选择两篇文献，最多8篇。</p>}
    {ready && <div className="comparison-preparation">{ready.cache_status === "hit" ? <p>可直接复用已完成的对比表，不调用模型；依据将与当前原文重新核对。</p> : <><StudyCoverage study={ready} /><p>本次单次请求上限估算 ¥{ready.estimated_upper_cny}，最大输出 {ready.max_output_tokens ?? 6000} tokens；不自动重试。{ready.pricing_basis}</p></>}</div>}
    {storageError && <p role="note">浏览器未能保存请求编号；已到达服务器的请求仍可从历史对比中查询。</p>}
    {history.length > 0 && <details><summary>本组近期对比 · {history.length} 条</summary>{history.map(item => <button key={item.request_id} disabled={busy} onClick={() => void operate("query", item.request_id)}>{item.question} · {new Date(item.created_at).toLocaleString()}</button>)}</details>}
    {error && <p role="alert">{error}</p>}
    {contextReady && receipt && <p role="status">{receipt.status === "completed" ? receipt.cache_hit ? "缓存表格 · 本次未调用模型" : "对比表已保存" : receipt.status === "running" ? "原请求仍在进行，请稍后查询，不会自动重发。" : receipt.status === "failed" ? `原请求失败：${ERRORS[receipt.error_code ?? ""] ?? "可能已产生费用，请先核对，不会自动重试。"}` : "原请求状态不明，不会自动重发。"} · 请求编号 {receipt.request_id}</p>}
    {result && <><div className="comparison-actions"><button onClick={() => downloadText("结构化对比.md", comparisonMarkdown(result, receipt!.source_changed))}>导出对比与依据（Markdown）</button><button onClick={() => downloadText("结构化对比.json", JSON.stringify({ format: "literature-workbench-comparison-v1", source_changed: receipt!.source_changed, scientific_review_required: true, quantity_warnings: result.rows.flatMap(row => Object.entries(row.cells).filter(([, cell]) => cell.status === "supported").map(([field, cell]) => ({ document_id: row.document_id, field, warnings: quantityWarnings(cell) }))), result }, null, 2), "application/json;charset=utf-8")}>导出对比与依据（JSON）</button></div><p>保存的问题：{result.question} · {result.model} · {new Date(result.created_at).toLocaleString()}。原句可定位，归纳待你核对。</p>{receipt?.source_changed && <p role="alert">来源已变化或不可用，以下是历史表格，请重新核对原文。</p>}{!result.cache_saved && <p role="note">对比表已保存，但逐篇分析缓存保存失败；后续新问题可能需要重读。</p>}
      <p role="note">当前表格有 {result.rows.flatMap(row => Object.values(row.cells)).filter(cell => cell.status === "supported").length} 格有可定位依据，{result.rows.flatMap(row => Object.values(row.cells)).filter(cell => cell.status === "unverified").length} 格未通过依据或数字核对并已留空。已填项均为待核对草稿。</p><div className="comparison-table-scroll"><table><thead><tr><th>文献</th>{Object.values(result.fields).map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{result.rows.map(row => <tr key={row.document_id}><th scope="row">{row.display_name}</th>{Object.keys(result.fields).map(field => { const cell = row.cells[field]; return <td key={field}>{cell?.status === "supported" ? <><p role="note">{REVIEW_FIELDS[field] ?? "请核对归纳与原文"}</p>{quantityWarnings(cell).length > 0 && <p role="alert">数字与单位未在引用中相邻匹配：{quantityWarnings(cell).join("、")}；请核对原文。</p>}{REVIEW_FIELDS[field] ? <details><summary>查看未经确认的 AI 草稿</summary><p>{cell.value}</p></details> : <p>{cell.value}</p>}{cell.citations.map((citation, i) => <details key={i} open={Boolean(REVIEW_FIELDS[field])}><summary>原文依据 · 第 {citation.physical_page} 页</summary><blockquote>{citation.excerpt}</blockquote><button onClick={() => { if (!receipt?.source_changed || window.confirm("来源已变化，仍打开当前文件对应页码核对历史引句吗？")) onEvidence({ ...citation, source_changed: receipt?.source_changed }); }}>定位原文</button></details>)}</> : <p className="comparison-missing">{cell?.status === "unverified" ? "依据或数字未通过核对，未填入" : "本次证据未覆盖"}</p>}</td>; })}</tr>)}</tbody></table></div></>}
  </section>;
}
