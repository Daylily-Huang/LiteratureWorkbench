import { useEffect, useRef, useState } from "react";
import type { CachedStudyRecord } from "./studyExport";
import { workspaceRequest } from "./workspaceApi";

export type StudyDraftReceipt = { id: string; document_ids: string[]; cache_hit: boolean; api_call_required: false };
export function StudyRelationDraft({ entry, claimIndex, onReview }: {
  entry: CachedStudyRecord; claimIndex: number; onReview(receipt: StudyDraftReceipt): void;
}) {
  const evidence = entry.claims[claimIndex]!.evidence;
  const [source, setSource] = useState(0);
  const [target, setTarget] = useState(() => evidence.findIndex(e => e.document_id !== evidence[0]?.document_id));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => { controller.current?.abort(); controller.current = null; }, []);
  const valid = entry.current && !entry.invalid && evidence[source]?.verified && evidence[target]?.verified && evidence[source]?.document_id !== evidence[target]?.document_id;
  if (new Set(evidence.map(e => e.document_id)).size < 2) return <small>此结论只有单篇依据，不能直接转成双篇关系。</small>;
  async function save() {
    if (!valid || controller.current) return;
    const request = new AbortController(); controller.current = request; setBusy(true); setError("");
    try {
      const result = await workspaceRequest<StudyDraftReceipt>("analyses/from-study", {
        cache_key: entry.cache_key, claim_index: claimIndex, source_index: source, target_index: target,
      }, "POST", request.signal);
      if (controller.current === request) onReview(result);
    } catch (e) {
      if (controller.current === request) setError(e instanceof Error && e.message === "study_evidence_changed"
        ? "原组正文或依据已变化，不能转入。请重新加载缓存核对，必要时重新研读。"
        : "尚未确认保存结果；可用同一对引句重试，不会重复建立候选。没有调用 AI。");
    } finally { if (controller.current === request) { controller.current = null; setBusy(false); } }
  }
  return <details className="study-relation-draft"><summary>将此结论送入关系审核 · 不调用 API</summary>
    <p>保留原研读组的完整语境，只将你选中的两条引句带入草稿。共同引用不代表支持或矛盾，关系需在审核中判断。</p>
    <label>左侧依据<select aria-label={`结论${claimIndex + 1}左侧依据`} value={source} disabled={busy} onChange={e => setSource(Number(e.target.value))}>{evidence.map((e, index) => <option key={index} value={index}>{index + 1}. {e.display_name} · 第 {e.physical_page} 页 · {e.excerpt.slice(0, 65)}</option>)}</select></label>
    <label>右侧依据<select aria-label={`结论${claimIndex + 1}右侧依据`} value={target} disabled={busy} onChange={e => setTarget(Number(e.target.value))}>{evidence.map((e, index) => <option key={index} value={index}>{index + 1}. {e.display_name} · 第 {e.physical_page} 页 · {e.excerpt.slice(0, 65)}</option>)}</select></label>
    <button type="button" disabled={!valid || busy} onClick={() => void save()}>{busy ? "正在核对并保存…" : "保存草稿并打开关系审核"}</button>
    {!valid && <small>需选择来自不同文献的两条有效引句，且原组全部正文仍一致。</small>}
    {error && <p role="alert">{error}</p>}
  </details>;
}
