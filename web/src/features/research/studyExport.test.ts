import { expect, it } from "vitest";
import { studyMarkdown, type CachedStudyRecord } from "./studyExport";

const entry: CachedStudyRecord = { cache_key: "cache-a", question: "A+B", model: "test-model", created_at: "2026-10-23", current: false, invalid: false, context_kind: "analysis_reuse",
  documents: [{ document_id: "a", display_name: "A.pdf", text_status: "current", read_mode: "analysis_reuse" }, { document_id: "b", display_name: "B.pdf", text_status: "changed", read_mode: "full_text" }],
  claims: [{ conclusion: "Comparison <script>alert(1)</script>", evidence: [
    { document_id: "a", display_name: "A.pdf", physical_page: 2, excerpt: "Original first line.\nSecond line.", content_id: "current-a", verified: true, rects: [] },
    { document_id: "b", display_name: "B.pdf", physical_page: 3, excerpt: "Older source quotation.", content_id: undefined, verified: false, rects: [] },
  ] }], limitations: ["Condition differences remain unresolved."] };

it("retains group identity, model, original quotations, physical pages and historical state", () => {
  const text = studyMarkdown(entry, "view-time");
  expect(text).toContain("历史分析"); expect(text).toContain("组内比较结论保留原范围");
  expect(text).toContain("A.pdf"); expect(text).toContain("B.pdf"); expect(text).toContain("第 2 页"); expect(text).toContain("第 3 页");
  expect(text).toContain("当前核对内容版本：current-a"); expect(text).toContain("历史引句，当前未核实");
  expect(text).toContain("> Original first line.\n> Second line."); expect(text).toContain("test-model"); expect(text).toContain("cache-a");
  expect(text).toContain("Condition differences remain unresolved."); expect(text).not.toContain("<script>");
});

it("refuses invalid analysis instead of exporting it as verified evidence", () => {
  expect(() => studyMarkdown({ ...entry, invalid: true }, "now")).toThrow("invalid_study_export");
});
