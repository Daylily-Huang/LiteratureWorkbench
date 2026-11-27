import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { StudyHistory } from "./StudyHistory";
import { LiteratureDesk } from "../library/LiteratureDesk";
import { AssistantPanel } from "./AssistantPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const evidence = { document_id: "a", display_name: "A.pdf", physical_page: 2, excerpt: "Original source quotation.", verified: true, content_id: "a".repeat(64), rects: [] };
const entry = { cache_key: "cache", question: "Compare A and B", created_at: "2026-10-23T00:00:00Z", model: "test", context_kind: "full_text", current: true, invalid: false,
  documents: [{ document_id: "a", display_name: "A.pdf", text_status: "current", read_mode: "full_text" }], claims: [{ conclusion: "Group conclusion", evidence: [evidence] }], limitations: ["Evidence gap remains."] };
const history = { display_name: "A.pdf", total: 1, has_more: false, items: [entry] };
const props = { documentId: "a", onClose: vi.fn(), onJump: vi.fn(), onReread: vi.fn() };
const docs = ["a", "b", "c"].map(id => ({ document_id: id, filename: `${id.toUpperCase()}.pdf`, relative_folder: "", authorization_source: "test" }));

it("shows cache evidence without an AI request, and prepares a reread only on explicit action", async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(history)));
  vi.stubGlobal("fetch", fetcher);
  render(<StudyHistory {...props} />);
  await screen.findByText("1. Group conclusion");
  expect(screen.getByText(evidence.excerpt)).toBeVisible();
  fireEvent.click(screen.getByText("原研读范围与版本"));
  expect(screen.getByText(/第 1 篇 · A.pdf/)).toBeVisible();
  expect(screen.getByText(/缓存查看 · 本次未调用 API/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "查看原文 · 第 2 页" }));
  expect(props.onJump).toHaveBeenCalledWith(evidence);
  fireEvent.click(screen.getByRole("button", { name: "重新研读此篇（API）" }));
  expect(props.onReread).toHaveBeenCalledWith("a");
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0]![0]).toBe("/api/libraries/full-text/studies/a?offset=0");
});

it("labels historical claims and prevents jumping to unverified coordinates", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...history, items: [{ ...entry, current: false, claims: [{ conclusion: "Old finding", evidence: [{ ...evidence, verified: false }] }] }] }))));
  render(<StudyHistory {...props} />);
  await screen.findByText(/这是历史分析/);
  expect(screen.getByRole("button", { name: "查看原文 · 第 2 页" })).toBeDisabled();
  expect(screen.getByText(/历史引句 · 当前未核实/)).toBeVisible();
});

it("ignores late previous-document responses and supports error retry", async () => {
  let finish!: (r: Response) => void;
  const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }))
    .mockResolvedValueOnce(new Response("", { status: 503 })).mockResolvedValueOnce(new Response(JSON.stringify({ ...history, display_name: "B.pdf" })));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<StudyHistory {...props} />);
  view.rerender(<StudyHistory {...props} documentId="b" />);
  await screen.findByRole("alert");
  await act(async () => finish(new Response(JSON.stringify(history))));
  expect(screen.queryByText("1. Group conclusion")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试加载缓存" }));
  await screen.findByRole("heading", { name: "B.pdf" });
});

it("lists verified, stale and absent history separately and filters without a paid call", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [
    { document_id: "a", status: "studied", history_count: 2, reusable: true }, { document_id: "b", status: "stale", history_count: 1, reusable: false },
  ] }))));
  const open = vi.fn();
  render(<LiteratureDesk documents={docs} records={{}} selected={[]} onSelect={vi.fn()} onOpen={vi.fn()} onUpdate={vi.fn()} onCompare={vi.fn()} onViewStudy={open} />);
  await screen.findByText("已全文研读 · 2 条");
  expect(screen.getByText("原文已变 · 待重读 · 1 条")).toBeVisible();
  fireEvent.change(screen.getByLabelText("AI研读状态筛选"), { target: { value: "studied" } });
  expect(screen.queryByRole("button", { name: "B.pdf" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "A.pdf 研读缓存" }));
  expect(open).toHaveBeenCalledWith("a");
});

it("reread drafts select full text and force refresh, require preflight, and reset for another scope", async () => {
  const fetcher = vi.fn();
  const selected = [{ document_id: "a", filename: "A.pdf" }];
  const view = render(<AssistantPanel document={null} selectedDocuments={selected} studyDraft={{ documentId: "a", nonce: 1 }} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  expect(screen.getByLabelText("跳过缓存，重新读全文（会调用 API）")).toBeChecked();
  expect(screen.getByLabelText("阅读方式")).toHaveValue("full_text");
  expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
  expect(fetcher).not.toHaveBeenCalled();
  view.rerender(<AssistantPanel document={null} selectedDocuments={[{ document_id: "b", filename: "B.pdf" }]} studyDraft={{ documentId: "a", nonce: 1 }} onJumpToCitation={vi.fn()} fetcher={fetcher} />);
  await waitFor(() => expect(screen.getByLabelText("跳过缓存，重新读全文（会调用 API）")).not.toBeChecked());
});
