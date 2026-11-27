import { markdownText } from "../ui/exportText";
import type { LibraryCitation } from "./AssistantPanel";

export type CachedStudyRecord = {
  cache_key: string; question: string; model: string; created_at: string; context_kind: string; current: boolean; invalid: boolean;
  documents: { document_id: string; display_name: string; text_status: string; read_mode: string }[];
  claims: { conclusion: string; evidence: (LibraryCitation & { verified: boolean })[] }[]; limitations: string[];
};
export function studyMarkdown(entry: CachedStudyRecord, viewedAt: string): string {
  if (entry.invalid) throw new Error("invalid_study_export");
  const text = markdownText;
  const lines = [`# ${text(entry.question)}`, `研读时间：${text(entry.created_at)}\n模型：${text(entry.model)}\n本次读取时间：${text(viewedAt)}\n缓存记录：${text(entry.cache_key)}`,
    "此为已保存的 AI 研读分析，查看和导出没有调用 AI。引句核对状态以本次读取时为准；AI 推论仍需核实。",
    entry.current ? "原研读范围的正文与依据本次核对一致。" : "历史分析：原组有来源变化、不可用或版本未核实，不能直接当作当前有效结论。",
    "## 原研读范围", ...entry.documents.map(doc => `- ${text(doc.display_name)} · 文献ID：${text(doc.document_id)} · ${doc.read_mode === "full_text" ? "当时读全文" : "当时复用分析/引句"} · ${doc.text_status === "current" ? "正文一致" : doc.text_status === "changed" ? "正文已变化" : "版本待核对"}`),
    "组内比较结论保留原范围，不改称某一篇的独立结论。", "## 分析与原文依据"];
  if (!entry.claims.length) lines.push("该次研读未形成有原文依据的结论。");
  entry.claims.forEach((claim, index) => {
    lines.push(`### ${index + 1}. ${text(claim.conclusion)}`);
    claim.evidence.forEach(e => lines.push(`《${text(e.display_name)}》· 第 ${e.physical_page} 页 · ${e.verified ? "原句已核对" : "历史引句，当前未核实"}\n文献ID：${text(e.document_id)}\n当前核对内容版本：${text(e.verified && e.content_id ? e.content_id : "未核实")}\n\n> ${text(e.excerpt).replace(/\r?\n/g, "\n> ")}`));
  });
  if (entry.limitations.length) lines.push("## 未解决问题与适用边界", ...entry.limitations.map(item => `- ${text(item)}`));
  return lines.join("\n\n");
}
