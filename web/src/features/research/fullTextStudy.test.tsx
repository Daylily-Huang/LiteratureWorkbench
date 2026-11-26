import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AssistantPanel } from "./AssistantPanel";
import { notebookMarkdown } from "./ResearchNotebook";
import { parseFullTextCoverage } from "./fullTextStudy";

afterEach(cleanup);
const selected = [{ document_id: "doc-a", filename: "A.pdf" }, { document_id: "doc-b", filename: "B.pdf" }];
const study = {
  plan_id: "a".repeat(64), cache_status: "miss", api_call_required: true, context_kind: "full_text",
  reading_mode: "full_text", text_only: true, source_versions: { "doc-a": "version-a", "doc-b": "version-b" },
  request_bytes: 18000, max_request_bytes: 262144,
  coverage: selected.map(d => ({ document_id: d.document_id, display_name: d.filename, content_id: "a".repeat(64), parse_run_id: "run", total_pages: 2, supplied_pages: 2, text_characters: 800 })),
};
const response = {
  answer: "Finding supported by exact original words.", provider: "deepseek", model: "test", indexed_documents: 2,
  citations: [{ document_id: "doc-a", display_name: "A.pdf", physical_page: 2, excerpt: "The original words on page two.", rects: [], content_id: "a".repeat(64) }],
  reading_mode: "full_text", reading_coverage: study, history_saved: true, entry_id: "entry",
};
function fakeFetcher() {
  return vi.fn(async (url: RequestInfo | URL) => {
    if (url === "/api/session/current") return new Response(JSON.stringify({ csrf_token: "csrf" }));
    if (url === "/api/libraries/full-text/prepare") return new Response(JSON.stringify(study));
    return new Response(JSON.stringify(response));
  });
}
it("defaults selected comparisons to full text, checks for free, then explicitly sends snapshots", async () => {
  const fetcher = fakeFetcher();
  render(<AssistantPanel document={null} selectedDocuments={selected} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  fireEvent.change(screen.getByLabelText("向所选文献提问"), { target: { value: "Compare conditions" } });
  expect(screen.getByLabelText("阅读方式")).toHaveValue("full_text");
  expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "检查全文" }));
  await screen.findByLabelText("全文检查结果");
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(fetcher.mock.calls.some(c => c[0] === "/api/libraries/ask")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "提问" }));
  await screen.findByText(response.answer);
  const ask = fetcher.mock.calls.find(c => c[0] === "/api/libraries/ask");
  expect(JSON.parse((ask as unknown as [string, RequestInit])[1].body as string)).toEqual({ question: "Compare conditions", document_ids: ["doc-a", "doc-b"], reading_mode: "full_text", source_versions: study.source_versions, plan_id: study.plan_id, reuse_analysis: true, force_refresh: false });
  expect(screen.getByLabelText("全文发送范围")).toBeInTheDocument();
  expect(screen.getByText(/本次回答没有引用以下论文/)).toHaveTextContent("B.pdf");
});
it("requires a new check when the question or selected papers change", async () => {
  const fetcher = fakeFetcher();
  const view = render(<AssistantPanel document={null} initialQuestion="Compare" selectedDocuments={selected} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  fireEvent.click(screen.getByRole("button", { name: "检查全文" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "提问" })).toBeEnabled());
  fireEvent.change(screen.getByLabelText("向所选文献提问"), { target: { value: "Different question" } });
  expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
  view.rerender(<AssistantPanel document={null} selectedDocuments={[selected[1]!]} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  expect(screen.queryByLabelText("全文检查结果")).not.toBeInTheDocument();
});
it("ignores late preflight results for a previous selection", async () => {
  let finish!: (r: Response) => void;
  const fetcher = vi.fn(async (url: RequestInfo | URL) => url === "/api/session/current" ? new Response(JSON.stringify({ csrf_token: "csrf" })) : new Promise<Response>(r => { finish = r; }));
  const view = render(<AssistantPanel document={null} initialQuestion="Compare" selectedDocuments={selected} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  fireEvent.click(screen.getByRole("button", { name: "检查全文" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  view.rerender(<AssistantPanel document={null} selectedDocuments={[selected[1]!]} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  finish(new Response(JSON.stringify(study)));
  await waitFor(() => expect(screen.getByRole("button", { name: "检查全文" })).toBeEnabled());
  expect(screen.queryByLabelText("全文检查结果")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
});
it("rejects partial coverage and retains full-text scope in exported notebook", () => {
  expect(parseFullTextCoverage({ ...study, coverage: [{ ...study.coverage[0], supplied_pages: 1 }] })).toBeNull();
  expect(parseFullTextCoverage({ ...study, coverage: [] })).toBeNull();
  const text = notebookMarkdown({ id: "e", title: "Question", question: "Compare", note: "", starred: false, revision: 0, created_at: "now", updated_at: "now", document_ids: ["doc-a", "doc-b"], answer: { ...response, reading_coverage: parseFullTextCoverage(study)! } });
  expect(text).toContain("A.pdf：2/2 页解析文字");
  expect(text).toContain("不含图片视觉理解");
  expect(text).toContain("The original words on page two.");
});

it("identifies free cached answers without emitting a model usage event", async () => {
  const hit = { ...study, request_bytes: 0, cache_status: "hit", api_call_required: false, cached_at: "2026-10-22T12:00:00Z" };
  const fetcher = vi.fn(async (url: RequestInfo | URL) => new Response(JSON.stringify(url === "/api/session/current" ? { csrf_token: "csrf" } : url === "/api/libraries/full-text/prepare" ? hit : { ...response, reading_coverage: hit })));
  const listener = vi.fn(); window.addEventListener("deepseek-usage-updated", listener);
  try {
    render(<AssistantPanel document={null} initialQuestion="Compare" selectedDocuments={selected} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
    fireEvent.click(screen.getByRole("button", { name: "检查全文" }));
    await screen.findByText(/可复用缓存回答/);
    fireEvent.click(screen.getByRole("button", { name: "提问" }));
    await screen.findByText(response.answer);
    expect(screen.getByLabelText("研读缓存状态")).toHaveTextContent("缓存回答 · 未调用 API");
    expect(listener).not.toHaveBeenCalled();
    const markdown = notebookMarkdown({ id: "e", title: "Cached", question: "Compare", note: "", starred: false, revision: 0, created_at: "now", updated_at: "now", document_ids: ["doc-a", "doc-b"], answer: { ...response, reading_coverage: parseFullTextCoverage(hit)! } });
    expect(markdown).toContain("缓存回答 · 未调用 API");
  } finally { window.removeEventListener("deepseek-usage-updated", listener); }
});

it("invalidates the prepared plan when the user explicitly skips caches", async () => {
  const fetcher = fakeFetcher();
  render(<AssistantPanel document={null} initialQuestion="Compare" selectedDocuments={selected} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  fireEvent.click(screen.getByRole("button", { name: "检查全文" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "提问" })).toBeEnabled());
  fireEvent.click(screen.getByRole("checkbox", { name: /跳过缓存/ }));
  expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "检查全文" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "提问" })).toBeEnabled());
  const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
  expect(JSON.parse(calls.filter(c => c[0] === "/api/libraries/full-text/prepare").at(-1)![1].body as string).force_refresh).toBe(true);
});

it("marks analysis reuse as a new request and clears changed sending plans", async () => {
  const reuse = { ...study, context_kind: "analysis_reuse", analysis_count: 1, request_bytes: 3000 };
  const fetcher = vi.fn(async (url: RequestInfo | URL) => new Response(JSON.stringify(url === "/api/session/current" ? { csrf_token: "csrf" } : url === "/api/libraries/full-text/prepare" ? reuse : { code: "full_text_plan_changed" }), { status: url === "/api/libraries/ask" ? 409 : 200 }));
  render(<AssistantPanel document={null} initialQuestion="New question" selectedDocuments={selected} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  fireEvent.click(screen.getByRole("button", { name: "检查全文" }));
  await screen.findByText(/新问题仍需调用 API/);
  fireEvent.click(screen.getByRole("button", { name: "提问" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("alert")).toHaveTextContent("本次未调用 AI");
  expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
});

it("shows exactly which old papers are reused and which new paper is read in an incremental comparison", async () => {
  const selectedThree = [...selected, { document_id: "doc-c", filename: "C.pdf" }];
  const mixed = { ...study, context_kind: "analysis_reuse", analysis_count: 1,
    source_versions: { ...study.source_versions, "doc-c": "version-c" },
    source_modes: { "doc-a": "analysis_reuse", "doc-b": "analysis_reuse", "doc-c": "full_text" },
    coverage: [...study.coverage, { ...study.coverage[0]!, document_id: "doc-c", display_name: "C.pdf" }] };
  const fetcher = vi.fn(async (url: RequestInfo | URL) => new Response(JSON.stringify(url === "/api/session/current" ? { csrf_token: "csrf" } : url === "/api/libraries/full-text/prepare" ? mixed : { ...response, indexed_documents: 3, reading_coverage: mixed })));
  render(<AssistantPanel document={null} initialQuestion="Compare with C" selectedDocuments={selectedThree} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  fireEvent.click(screen.getByRole("button", { name: "检查全文" }));
  const check = await screen.findByLabelText("全文检查结果");
  expect(check).toHaveTextContent("旧篇复用，仅新篇/变化篇读全文");
  const rows = check.querySelectorAll("li");
  expect(rows[0]).toHaveTextContent("A.pdf"); expect(rows[0]).toHaveTextContent("本次：复用分析/引句");
  expect(rows[1]).toHaveTextContent("本次：复用分析/引句"); expect(rows[2]).toHaveTextContent("本次：读全文");
  fireEvent.click(screen.getByRole("button", { name: "提问" }));
  await screen.findByText(response.answer);
  expect(screen.getByLabelText("研读缓存状态")).toHaveTextContent("增量研读对比 · 本次调用 API");
  const markdown = notebookMarkdown({ id: "e", title: "Incremental", question: "Compare", note: "", starred: false, revision: 0, created_at: "now", updated_at: "now", document_ids: ["doc-a", "doc-b", "doc-c"], answer: { ...response, reading_coverage: parseFullTextCoverage(mixed)! } });
  expect(markdown).toContain("增量研读对比"); expect(markdown).toContain("本次：复用分析与引句"); expect(markdown).toContain("本次：全文");
  expect(parseFullTextCoverage({ ...mixed, source_modes: { "doc-a": "analysis_reuse" } })).toBeNull();
});


it("numbers the selected papers and invalidates a free preflight when their order reverses", async () => {
  const fetcher = fakeFetcher();
  const props = { document: null, initialQuestion: "Does the first support the second?", onJumpToCitation: vi.fn(), fetcher };
  const view = render(<AssistantPanel {...props} selectedDocuments={selected} />);
  fireEvent.click(screen.getByText("本次研读范围 · 2 篇"));
  expect(screen.getAllByRole("listitem").map(item => item.textContent)).toEqual(["A.pdf", "B.pdf"]);
  fireEvent.click(screen.getByRole("button", { name: "检查全文" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "提问" })).toBeEnabled());
  view.rerender(<AssistantPanel {...props} selectedDocuments={[selected[1]!, selected[0]!]} />);
  expect(screen.queryByLabelText("全文检查结果")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
  expect(screen.getAllByRole("listitem").map(item => item.textContent)).toEqual(["B.pdf", "A.pdf"]);
  expect(fetcher.mock.calls.some(call => call[0] === "/api/libraries/ask")).toBe(false);
});
