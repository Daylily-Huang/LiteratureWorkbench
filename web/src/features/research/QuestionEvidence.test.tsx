import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { QuestionEvidence, evidenceMarkdown } from "./QuestionEvidence";
import type { GraphData, GraphEdge } from "./graphModel";
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const edge: GraphEdge = { id: "r", relation_id: "r", source: "doc:a", target: "doc:b", kind: "manual", label: "存在分歧", relation: "contradicts", question: "Sampling effects?", difference_basis: "sample", comparison_note: "Adults versus juveniles", note: "Different estimates", source_page: 2, target_page: 8, source_content_id: "old-a", target_content_id: "old-b", source_changed: true };
const data: GraphData = { history_truncated: false, nodes: ["a", "b", "c"].map(id => ({ id: "doc:" + id, label: id + ".pdf", kind: "document" })), edges: [edge, { ...edge, id: "other", target: "doc:c", question: "Another question", difference_basis: "method" }] };
test("question evidence retains scope, guards changed sources and exposes question navigation", () => {
  const pair = vi.fn(), graph = vi.fn();
  render(<QuestionEvidence data={data} scope={["a", "b"]} onPair={pair} onGraph={graph} onDelete={vi.fn()} onRead={vi.fn()} />);
  expect(screen.queryByText("Another question")).toBeNull();
  expect(screen.getByText("Adults versus juveniles")).toBeVisible();
  vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(screen.getByRole("button", { name: "对照两处原文" })); expect(pair).not.toHaveBeenCalled();
  vi.mocked(window.confirm).mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "对照两处原文" })); expect(pair).toHaveBeenCalledWith(edge);
  fireEvent.click(screen.getByRole("button", { name: "在图谱中查看问题" })); expect(graph).toHaveBeenCalledWith("Sampling effects?");
  fireEvent.change(screen.getByLabelText("筛选差异来源"), { target: { value: "method" } });
  expect(screen.queryByText("Adults versus juveniles")).toBeNull();
  expect(screen.getByRole("button", { name: "导出当前对照" })).toBeDisabled();
});
test("export preserves direction, both versions and manual/unverified status", () => {
  const text = evidenceMarkdown([edge], data);
  for (const value of ["Sampling effects?", "a.pdf", "b.pdf", "old-a", "old-b", "第 2 页", "第 8 页", "人工记录", "已变化", "Adults versus juveniles", "样本或研究对象不同"]) expect(text).toContain(value);
  expect(text).not.toContain("c.pdf");
});
