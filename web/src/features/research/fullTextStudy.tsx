export type FullTextCoverage = {
  reading_mode: "full_text";
  coverage: { document_id: string; display_name: string; content_id: string; parse_run_id: string; total_pages: number; supplied_pages: number; text_characters: number }[];
  source_versions: Record<string, string>;
  request_bytes: number;
  max_request_bytes: number;
  text_only: true;
  cache_status?: "hit" | "miss";
  api_call_required?: boolean;
  context_kind?: "full_text" | "analysis_reuse";
  analysis_count?: number;
  cached_at?: string | null;
  plan_id?: string;
  cache_saved?: boolean;
  evidence_pages?: number;
  source_modes?: Record<string, "full_text" | "analysis_reuse">;
};

export function parseFullTextCoverage(value: unknown): FullTextCoverage | null {
  if (!value || typeof value !== "object") return null;
  const x = value as Record<string, unknown>;
  if (x.reading_mode !== "full_text" || x.text_only !== true || !Array.isArray(x.coverage) || !x.coverage.length || x.coverage.length > 8 ||
      !x.source_versions || typeof x.source_versions !== "object" || Array.isArray(x.source_versions) ||
      typeof x.request_bytes !== "number" || !Number.isSafeInteger(x.request_bytes) || x.request_bytes < 0 ||
      (x.request_bytes === 0 && !(x.cache_status === "hit" && x.api_call_required === false)) ||
      typeof x.max_request_bytes !== "number" || x.request_bytes > x.max_request_bytes) return null;
  const versions = x.source_versions as Record<string, unknown>;
  if (x.cache_status !== undefined && x.cache_status !== "hit" && x.cache_status !== "miss") return null;
  if (x.context_kind !== undefined && x.context_kind !== "full_text" && x.context_kind !== "analysis_reuse") return null;
  if (x.cache_status === "hit" && (x.api_call_required !== false || x.request_bytes !== 0)) return null;
  if (x.cache_status === "miss" && (x.api_call_required !== true || x.request_bytes === 0)) return null;
  const ids = new Set<string>();
  for (const value of x.coverage) {
    if (!value || typeof value !== "object") return null;
    const d = value as Record<string, unknown>;
    if (typeof d.document_id !== "string" || !d.document_id || ids.has(d.document_id) ||
        typeof d.display_name !== "string" || typeof d.content_id !== "string" || typeof d.parse_run_id !== "string" ||
        typeof d.total_pages !== "number" || !Number.isSafeInteger(d.total_pages) || d.total_pages < 1 || d.supplied_pages !== d.total_pages ||
        typeof d.text_characters !== "number" || !Number.isSafeInteger(d.text_characters) || d.text_characters < 1 ||
        typeof versions[d.document_id] !== "string") return null;
    ids.add(d.document_id);
  }
  if (Object.keys(versions).length !== ids.size) return null;
  if (x.source_modes !== undefined) {
    if (!x.source_modes || typeof x.source_modes !== "object" || Array.isArray(x.source_modes)) return null;
    const modes = x.source_modes as Record<string, unknown>;
    if (Object.keys(modes).length !== ids.size || [...ids].some(id => modes[id] !== "full_text" && modes[id] !== "analysis_reuse")) return null;
  }
  return value as FullTextCoverage;
}

export function coverageText(study: FullTextCoverage): string {
  return study.coverage.map(d => `${d.display_name}：${d.supplied_pages}/${d.total_pages} 页解析文字，${d.text_characters} 字符${study.source_modes ? ` · ${study.cache_status === "hit" ? "原分析方式：" : "本次："}${study.source_modes[d.document_id] === "full_text" ? "全文" : "复用分析与引句"}` : ""}`).join("；");
}

export function isIncrementalStudy(study?: FullTextCoverage | null): boolean {
  const modes = Object.values(study?.source_modes ?? {});
  return modes.includes("full_text") && modes.includes("analysis_reuse");
}

export function studyUsageText(study: FullTextCoverage, sent = true): string {
  if (study.cache_status === "hit") return sent ? "缓存回答 · 未调用 API" : "可复用缓存回答 · 不调用 API";
  if (isIncrementalStudy(study)) return sent ? "增量研读对比 · 本次调用 API" : "旧篇复用，仅新篇/变化篇读全文 · 提问将调用 API";
  if (study.context_kind === "analysis_reuse") return sent ? "复用分析生成 · 本次调用 API" : "将复用已有分析 · 新问题仍需调用 API";
  return sent ? "新全文分析 · 本次调用 API" : "全文检查通过 · 提问将调用 API";
}

export function StudyCoverage({ study, sent = false }: { study: FullTextCoverage; sent?: boolean }) {
  return <details className="assistant-selection" aria-label={sent ? "全文发送范围" : "全文检查结果"}>
    <summary>{study.cache_status || study.context_kind ? studyUsageText(study, sent) : sent ? "已提供全部解析文字" : "全文检查通过"} · {study.coverage.length} 篇 / {study.coverage.reduce((n, d) => n + d.total_pages, 0)} 页</summary>
    <ul>{study.coverage.map(d => <li key={d.document_id}>{d.display_name} · {d.supplied_pages}/{d.total_pages} 页 · {d.text_characters.toLocaleString()} 字符{study.source_modes && <strong> · {study.cache_status === "hit" ? "原分析：" : "本次："}{study.source_modes[d.document_id] === "full_text" ? "读全文" : "复用分析/引句"}</strong>}</li>)}</ul>
    <small>{study.cache_status === "hit" ? `本次未发送正文，复用时间：${study.cached_at ? new Date(study.cached_at).toLocaleString() : "已保存"}。` : isIncrementalStudy(study) ? `本次仅为清单中标为“读全文”的文献提供全部解析文字；旧篇仅提供已有分析和核验引句。合并对比仍需一次 API 请求，约 ${(study.request_bytes / 1024).toFixed(1)} KB。` : study.context_kind === "analysis_reuse" ? `本次只发送 ${study.analysis_count ?? 0} 次已有研读的分析笔记和核验过的原句，约 ${(study.request_bytes / 1024).toFixed(1)} KB，并未重新发送全文。证据不足时可主动跳过缓存重读。` : `本次提供全部解析文字，请求约 ${(study.request_bytes / 1024).toFixed(1)} KB。暂无可用分析或引句的篇也需要读全文。`}覆盖指原文版本的解析文字，不含图片视觉理解，也不保证表格和公式解析无误。</small>
  </details>;
}
