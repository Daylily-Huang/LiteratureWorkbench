import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { StructuredComparison, comparisonMarkdown, quantityWarnings, type ComparisonResult } from "./StructuredComparison";
import { workspaceRequest } from "./workspaceApi";
vi.mock("./workspaceApi", () => ({ workspaceRequest: vi.fn() }));
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); });
const citation = { document_id: "a", display_name: "Alpha.pdf", content_id: "original-a", physical_page: 2, excerpt: "Twelve sites were surveyed repeatedly.", rects: [] };
const result: ComparisonResult = { question: "Compare", model: "test-model", created_at: "2026-10-24T00:00:00Z", fields: { sample: "样本量", method: "方法" }, rows: [{ document_id: "a", display_name: "Alpha.pdf", content_id: "original-a", cells: { sample: { status: "supported", value: "12 sites", citations: [citation] }, method: { status: "insufficient", value: "", citations: [] } } }], coverage: {} as ComparisonResult["coverage"], cache_saved: true, source_versions: {} };
const ready = { plan_id: "plan", source_versions: { a: "v1", b: "v2" }, coverage: [{ document_id: "a" }, { document_id: "b" }], cache_status: "hit", estimated_upper_cny: "0" };
function setup() {
  vi.mocked(workspaceRequest).mockReset();
  vi.mocked(workspaceRequest).mockImplementation(async path => {
    if (path === "comparisons") return { workspace_id: "workspace", items: [] } as never;
    if (path.endsWith("prepare")) return ready as never;
    return {} as never;
  });
  const evidence = vi.fn(); render(<StructuredComparison ids={["a", "b"]} onEvidence={evidence} onBusy={vi.fn()} />);return evidence;
}

it("requires free preparation, renders quoted cells and leaves unsupported fields empty", async () => {
  const evidence=setup();await waitFor(()=>expect(screen.getByRole("button",{name:"免费检查范围与费用"})).toBeEnabled());
  expect(screen.getByRole("button",{name:"生成结构化对比表（API）"})).toBeDisabled();
  fireEvent.click(screen.getByRole("button",{name:"免费检查范围与费用"}));
  const button=await screen.findByRole("button",{name:"读取缓存对比表"});
  vi.mocked(workspaceRequest).mockResolvedValue({ request_id: "r", status: "completed", cache_hit: true, result, source_changed: false } as never);
  fireEvent.click(button);await screen.findByText("12 sites");
  expect(screen.getByText("本次证据未覆盖")).toBeVisible();
  expect(screen.getByText("请核对样本量、统计对象和单位")).toBeVisible();
  expect(screen.getByText("Twelve sites were surveyed repeatedly.")).toBeVisible();
  expect(screen.getByText("12 sites")).not.toBeVisible();
  fireEvent.click(screen.getByText("查看未经确认的 AI 草稿"));
  expect(screen.getByText("12 sites")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"定位原文"}));
  expect(evidence).toHaveBeenCalledWith({...citation,source_changed:false});
  const text=comparisonMarkdown(result,true);for(const value of ["original-a","第 2 页","Twelve sites","历史来源已变化","不代表原文未报告"])expect(text).toContain(value);
});

it("keeps the actual omitted condition visible before showing the cached model draft and in export", async () => {
  setup(); await waitFor(()=>expect(screen.getByRole("button",{name:"免费检查范围与费用"})).toBeEnabled());
  fireEvent.click(screen.getByRole("button",{name:"免费检查范围与费用"}));
  const button=await screen.findByRole("button",{name:"读取缓存对比表"});
  const text="estimates should be taken solely as approximate indices of abundance when sampling is not continuous over the population range";
  const conditionResult: ComparisonResult = { ...result, fields: { limitations: "作者指出的局限" }, rows: [{ ...result.rows[0]!, cells: { limitations: { status: "supported", value: "建议非空间 CR 仅作粗略指数", citations: [{...citation, excerpt:text}] } } }] };
  vi.mocked(workspaceRequest).mockResolvedValue({ request_id:"r", status:"completed", cache_hit:true, result:conditionResult, source_changed:false } as never);
  fireEvent.click(button);
  expect(await screen.findByText(text)).toBeVisible();
  expect(screen.getByText("请核对局限和建议的适用条件")).toBeVisible();
  expect(screen.getByText("建议非空间 CR 仅作粗略指数")).not.toBeVisible();
  expect(comparisonMarkdown(conditionResult,false)).toContain(text);
  expect(comparisonMarkdown(conditionResult,false)).toContain("未经人工确认的 AI 草稿");
});

it("flags a swapped sample unit and area unit without claiming all scientific meaning is verified", () => {
  const cell = { status: "supported" as const, value: "411个体，5.0公顷", citations: [{ ...citation, excerpt: "411 samples from 209 individuals; density was 5.0 km²." }] };
  expect(quantityWarnings(cell)).toEqual(["411 个体", "5.0 公顷"]);
  expect(quantityWarnings({ ...cell, value: "411 samples, 209 individuals" })).toEqual([]);
});

it("changing question invalidates a free plan and late results do not prepare a new scope", async () => {
  setup();await waitFor(()=>expect(screen.getByRole("button",{name:"免费检查范围与费用"})).toBeEnabled());
  fireEvent.click(screen.getByRole("button",{name:"免费检查范围与费用"}));await screen.findByRole("button",{name:"读取缓存对比表"});
  fireEvent.change(screen.getByLabelText("结构化比较问题"),{target:{value:"New question"}});
  expect(screen.getByRole("button",{name:"生成结构化对比表（API）"})).toBeDisabled();
  expect(vi.mocked(workspaceRequest).mock.calls.filter(([,body])=>body && "request_id" in (body as object))).toHaveLength(0);
});

it("lost receipt on a forced request can only query the same ID unless explicitly reset", async () => {
  setup();await waitFor(()=>expect(screen.getByRole("button",{name:"免费检查范围与费用"})).toBeEnabled());
  fireEvent.click(screen.getByRole("checkbox"));fireEvent.click(screen.getByRole("button",{name:"免费检查范围与费用"}));await screen.findByRole("button",{name:"读取缓存对比表"});
  vi.mocked(workspaceRequest).mockRejectedValueOnce(new Error("lost_response"));
  fireEvent.click(screen.getByRole("button",{name:"读取缓存对比表"}));await screen.findByRole("alert");
  const sent=vi.mocked(workspaceRequest).mock.calls.filter(([,body])=>body && "request_id" in (body as object));expect(sent).toHaveLength(1);
  vi.mocked(workspaceRequest).mockResolvedValueOnce({request_id:"r",status:"unknown",result:null} as never);
  fireEvent.click(screen.getByRole("button",{name:"读取缓存对比表"}));await screen.findByText(/原请求状态不明/);
  expect(vi.mocked(workspaceRequest).mock.calls.filter(([,body])=>body && "request_id" in (body as object))).toHaveLength(1);
  expect(vi.mocked(workspaceRequest).mock.calls.at(-1)?.[0]).toContain("comparisons/");
});
