import type { PdfAnnotation } from "./annotations";
import { markdownText } from "../ui/exportText";

export function exportNotes(filename: string, items: PdfAnnotation[]): string {
  const lines = [`# ${markdownText(filename)} · 阅读笔记`, "", "来源：PDF 中的批注与摘录。页码为 PDF 物理页码。", ""];
  for (const item of [...items].sort((a, b) => a.page - b.page)) {
    lines.push(`## 第 ${item.page} 页 · ${item.type === "comment" ? "评论" : item.type === "underline" ? "下划线" : "高亮"}`, "");
    if (item.selected_text) lines.push(...item.selected_text.split(/\r?\n/).map(line => `> ${markdownText(line)}`), "");
    if (item.comment) lines.push(`笔记：${markdownText(item.comment)}`, "");
    if (item.read_only) lines.push("（历史批注，只读）", "");
  }
  return lines.join("\n");
}
