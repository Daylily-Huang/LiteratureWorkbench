import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { RelationReview, type RelationAnalysis } from "./RelationReview";

afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const docs = ["a", "b"].map(id => ({ document_id: id, filename: id + ".pdf", relative_folder: "", authorization_source: "test" }));
function fixture(): RelationAnalysis {
  return { id: "analysis", question: "Sampling design?", document_ids: ["a", "b"], provider: "test", model: "test", fallback_reason: null, summary: "Design differs", warnings: ["Check populations"], coverage: docs.map(d => ({ document_id: d.document_id, display_name: d.filename, retrieved: true, has_candidate: true })), created_at: "2026-10-18T00:00:00Z", candidates: [{ id: "candidate", status: "pending", revision: 0, source_changed: false, relation_id: null, review: null, proposal: { source: { document_id: "a", display_name: "a.pdf", physical_page: 2, excerpt: "Repeated sampling source quote.", rects: [] }, target: { document_id: "b", display_name: "b.pdf", physical_page: 3, excerpt: "Single visit sampling source quote.", rects: [] }, relation: "method", difference_basis: "method", rationale: "AI original rationale", comparison_note: "Compare detection assumptions", caveats: ["Unknown sample conditions"] } }] };
}

test("opens the requested cached-study draft even when it is outside the latest history page", async () => {
  const wanted = { ...fixture(), id: "wanted", question: "Original cached-study context" };
  wanted.candidates[0]!.status = "accepted";
  wanted.candidates[0]!.relation_id = "existing-edge";
  vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith("/analyses/wanted") ? { items: [wanted] } : { items: [{ ...fixture(), id: "newer" }], truncated: false }))));
  render(<RelationReview ids={["a", "b"]} documents={docs} initialAnalysisId="wanted" onPair={vi.fn()} onBusy={vi.fn()} onDirty={vi.fn()} onGraphChanged={vi.fn()} />);
  expect(await screen.findByText("已确认加入图谱")).toBeVisible();
  expect(screen.getByLabelText("候选审核状态")).toHaveValue("all");
  expect(screen.getByLabelText("选择分析记录")).toHaveValue("wanted");
});
test("never accepts automatically; edits revoke checkbox and exact user decision is sent once", async () => {
  const analysis = fixture();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/session/current")) return new Response(JSON.stringify({ csrf_token: "csrf" }));
    if (url.endsWith("/accept")) { analysis.candidates[0]!.status = "accepted"; analysis.candidates[0]!.relation_id = "relation"; return new Response(JSON.stringify({ relation_id: "relation" })); }
    return new Response(JSON.stringify({ items: [analysis], truncated: false }));
  }); vi.stubGlobal("fetch", fetcher);
  const pair = vi.fn(), changed = vi.fn(async () => {});
  render(<RelationReview ids={["a", "b"]} documents={docs} onPair={pair} onBusy={vi.fn()} onDirty={vi.fn()} onGraphChanged={changed} />);
  await screen.findByText("Repeated sampling source quote.");
  expect(fetcher.mock.calls.some(c => c[1]?.method === "POST")).toBe(false);
  const accept = screen.getByRole("button", { name: "确认并加入图谱" }); expect(accept).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "核对两侧原文" })); expect(pair).toHaveBeenCalledWith(expect.objectContaining({ source: "doc:a", target: "doc:b", source_page: 2, target_page: 3 }));
  const confirm = screen.getByRole("checkbox", { name: "我已核对两侧原文，同意将当前修订后的关系加入图谱" }); fireEvent.click(confirm); expect(accept).toBeEnabled();
  fireEvent.change(screen.getByLabelText("审核关系依据"), { target: { value: "My corrected interpretation" } }); expect(confirm).not.toBeChecked(); expect(accept).toBeDisabled();
  fireEvent.click(confirm); fireEvent.click(accept);
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  const requests = fetcher.mock.calls.filter(c => c[0].endsWith("/accept")); expect(requests).toHaveLength(1);
  expect(JSON.parse(requests[0]![1]!.body as string)).toMatchObject({ confirmed: true, revision: 0, note: "My corrected interpretation", relation: "method" });
});
test("stale candidate cannot be accepted and histories stay within selection", async () => {
  const analysis = fixture(); analysis.candidates[0]!.source_changed = true;
  const other = { ...fixture(), id: "other", question: "Outside selection", document_ids: ["a", "c"] };
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [analysis, other], truncated: false }))));
  render(<StrictMode><RelationReview ids={["a", "b"]} documents={docs} onPair={vi.fn()} onBusy={vi.fn()} onDirty={vi.fn()} onGraphChanged={vi.fn()} /></StrictMode>);
  await screen.findByText("Repeated sampling source quote.");
  expect(screen.getByRole("checkbox", { name: "我已核对两侧原文，同意将当前修订后的关系加入图谱" })).toBeDisabled(); expect(screen.getByRole("button", { name: "确认并加入图谱" })).toBeDisabled();
  expect(screen.queryByText("Outside selection")).toBeNull();
});
test("analysis request uses explicit selected papers and does not call graph mutation", async () => {
  const calls: { url: string; body: unknown }[] = []; let generated = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/session/current")) return new Response(JSON.stringify({ csrf_token: "csrf" }));
    if (init?.method === "POST") { calls.push({ url, body: JSON.parse(init.body as string) }); generated = true; return new Response(JSON.stringify({ id: "analysis", status: "completed", candidate_count: 1, request_id: (JSON.parse(init.body as string) as { request_id: string }).request_id })); }
    return new Response(JSON.stringify({ items: generated ? [fixture()] : [], truncated: false, workspace_id: "test-workspace" }));
  }));
  render(<RelationReview ids={["a", "b"]} documents={docs} onPair={vi.fn()} onBusy={vi.fn()} onDirty={vi.fn()} onGraphChanged={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("AI 关系分析问题"), { target: { value: "Sampling design?" } });
  fireEvent.change(screen.getByLabelText("关系分析阅读方式"), { target: { value: "retrieval" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "分析所选文献关系" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "分析所选文献关系" }));
  await screen.findByText("分析已保存：1 条可审核建议。尚未加入图谱。");
  expect(calls).toEqual([{ url: "/api/workspace/analyses", body: { question: "Sampling design?", document_ids: ["a", "b"], request_id: expect.any(String), force_refresh: false } }]);
});

test("refreshing newer analyses retains the current candidate draft and new requests respect its leave guard", async () => {
  const original = fixture(); let newer = false;
  const fresh = { ...fixture(), id: "new-analysis", question: "Newer question", candidates: [{ ...fixture().candidates[0]!, id: "new-candidate" }] };
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify({ items: newer ? [fresh, original] : [original], truncated: false, workspace_id: "workspace" }))); vi.stubGlobal("fetch", fetcher);
  render(<RelationReview ids={["a", "b"]} documents={docs} onPair={vi.fn()} onBusy={vi.fn()} onDirty={vi.fn()} onGraphChanged={vi.fn()} />);
  await screen.findByText("Repeated sampling source quote.");
  fireEvent.change(screen.getByLabelText("审核关系依据"), { target: { value: "Unsaved human correction" } });
  newer = true; fireEvent.click(screen.getByRole("button", { name: "刷新候选记录" }));
  await screen.findByRole("option", { name: /Newer question/ });
  expect(screen.getByLabelText("审核关系依据")).toHaveValue("Unsaved human correction");
  fireEvent.change(screen.getByLabelText("AI 关系分析问题"), { target: { value: "Another question" } });
  vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.change(screen.getByLabelText("关系分析阅读方式"), { target: { value: "retrieval" } });
  fireEvent.click(screen.getByRole("button", { name: "分析所选文献关系" }));
  expect(window.confirm).toHaveBeenCalled(); expect(fetcher.mock.calls.some(call => call[1]?.method === "POST")).toBe(false);
  expect(screen.getByLabelText("审核关系依据")).toHaveValue("Unsaved human correction");
});


test("initial paper-order change loads the current workspace and ignores a late old failure", async () => {
  let rejectOld!: (reason: Error) => void;
  let reads = 0;
  const fetcher = vi.fn((_url: string, _init?: RequestInit) => {
    reads++;
    if (reads === 1) return new Promise<Response>((_resolve, reject) => { rejectOld = reject; });
    return Promise.resolve(new Response(JSON.stringify({ items: [], truncated: false, workspace_id: "workspace" })));
  }); vi.stubGlobal("fetch", fetcher);
  const props = { documents: docs, onPair: vi.fn(), onBusy: vi.fn(), onDirty: vi.fn(), onGraphChanged: vi.fn(async () => {}) };
  const view = render(<RelationReview {...props} ids={["a", "b"]} />);
  fireEvent.change(screen.getByLabelText("AI 关系分析问题"), { target: { value: "Does the first support the second?" } });
  expect(screen.getByRole("button", { name: "分析所选文献关系" })).toBeDisabled();
  view.rerender(<RelationReview {...props} ids={["b", "a"]} />);
  fireEvent.change(screen.getByLabelText("关系分析阅读方式"), { target: { value: "retrieval" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "分析所选文献关系" })).toBeEnabled());
  rejectOld(new Error("late old scope error"));
  await Promise.resolve(); await Promise.resolve();
  expect(screen.queryByText(/late old scope error/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "分析所选文献关系" })).toBeEnabled();
  expect(fetcher.mock.calls.some(call => call[1]?.method === "POST")).toBe(false);
});
