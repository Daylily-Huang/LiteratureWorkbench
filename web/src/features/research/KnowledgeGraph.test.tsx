import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { KnowledgeGraph } from "./KnowledgeGraph";
import type { GraphData, GraphEdge } from "./graphModel";
import { downloadText } from "../ui/exportText";

vi.mock("../ui/exportText", () => ({ downloadText: vi.fn() }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); });
const edge: GraphEdge = { id: "pair", relation_id: "r", source: "doc:a", target: "doc:b", kind: "manual", relation: "contradicts", label: "存在分歧", question: "Sampling effort?", comparison_note: "Adults versus juveniles", note: "Density estimates differ", source_changed: true, source_page: 2, target_page: 4 };
const data: GraphData = { history_truncated: false, nodes: ["a", "b", "c"].map(id => ({ id: `doc:${id}`, document_id: id, kind: "document", label: `${id}.pdf` })), edges: [edge, { ...edge, id: "method", source: "doc:b", target: "doc:c", relation: "method", label: "方法对照", question: "Other methods?", source_changed: false }] };
function setup(scope: string[] = []) {
  const pair = vi.fn();
  render(<KnowledgeGraph data={data} scope={scope} onOpen={vi.fn()} onPair={pair} onNotebook={vi.fn()} onGroup={vi.fn()} onDeleteRelation={vi.fn()} />);
  return pair;
}

test("search finds relation evidence, preserves connected papers and guards changed originals", () => {
  const pair = setup();
  fireEvent.change(screen.getByLabelText("搜索图谱节点"), { target: { value: "sampling juveniles" } });
  expect(screen.getByText(/2 个节点 · 1 条关系/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "论文：c.pdf" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "论文：a.pdf" }));
  expect(screen.getByText("研究问题：Sampling effort?")).toBeVisible();
  vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(screen.getByRole("button", { name: "对照两处原文" }));
  expect(pair).not.toHaveBeenCalled();
  vi.mocked(window.confirm).mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "对照两处原文" }));
  expect(pair).toHaveBeenCalledWith(edge);
});

test("filter and export retain complete provenance and restore without changing the graph", () => {
  setup();
  fireEvent.change(screen.getByLabelText("筛选文献关系类型"), { target: { value: "contradicts" } });
  fireEvent.change(screen.getByLabelText("筛选图谱来源状态"), { target: { value: "changed" } });
  fireEvent.click(screen.getByRole("button", { name: "导出图谱" }));
  const exported = JSON.parse(vi.mocked(downloadText).mock.calls[0]![1]);
  expect(exported.edges).toEqual([edge]);
  expect(exported.nodes.map((n: { id: string }) => n.id)).toEqual(["doc:a", "doc:b"]);
  fireEvent.change(screen.getByLabelText("搜索图谱节点"), { target: { value: "no result" } });
  expect(screen.getByText(/没有匹配的节点或关系/)).toBeVisible();
  expect(screen.queryByText(/还没有这类关系/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "清除搜索与筛选" }));
  expect(screen.getByText(/3 个节点 · 2 条关系/)).toBeVisible();
  expect(data.edges).toHaveLength(2);
});

test("search never imports an excluded paper and organization view resets incompatible filters", () => {
  setup(["a", "b"]);
  fireEvent.change(screen.getByLabelText("搜索图谱节点"), { target: { value: "Other methods" } });
  expect(screen.getByText(/没有匹配的节点或关系/)).toBeVisible();
  fireEvent.change(screen.getByLabelText("筛选文献关系类型"), { target: { value: "method" } });
  fireEvent.change(screen.getByLabelText("图谱关系筛选"), { target: { value: "organization" } });
  expect(screen.getByLabelText("搜索图谱节点")).toHaveValue("");
  expect(screen.queryByLabelText("筛选文献关系类型")).toBeNull();
  expect(screen.getByText(/还没有这类关系/)).toBeVisible();
});
