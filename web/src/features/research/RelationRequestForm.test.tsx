import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { RelationRequestForm } from "./RelationRequestForm";
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const ids = ["a", "b"];
const done = (request_id: string, extra = {}) => ({ request_id, status: "completed", id: "saved-analysis", candidate_count: 1, ...extra });
function Form({ workspace = "workspace", complete = vi.fn(async () => {}), initial = "Compare methods", papers = ids, busy = vi.fn() }: { workspace?: string; papers?: string[]; complete?: (receipt: unknown, question: string) => Promise<void>; initial?: string; busy?: (value: boolean) => void }) {
  const [question, setQuestion] = useState(initial);
  return <RelationRequestForm initialReadingMode="retrieval" ids={papers} workspace={workspace} question={question} onQuestion={setQuestion} disabled={false} onBusy={busy} onComplete={complete} />;
}
const submit = () => fireEvent.click(screen.getByRole("button", { name: "分析所选文献关系" }));

test("lost receipt is recovered with the persisted request ID and repeat clicks only query", async () => {
  const complete = vi.fn(async () => {}); let identifier = "";
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/session/current")) return new Response(JSON.stringify({ csrf_token: "csrf" }));
    if (init?.method === "POST") {
      const body = JSON.parse(init.body as string); identifier = body.request_id;
      expect(JSON.parse(localStorage.getItem(`lw-relation-attempt-v1:workspace:${identifier}`)!)).toMatchObject({ request_id: identifier, question: "Compare methods" });
      throw new Error("reply lost after commit");
    }
    expect(url).toContain(identifier);
    return new Response(JSON.stringify(done(identifier, { readonly: true })));
  }); vi.stubGlobal("fetch", fetcher);
  render(<Form complete={complete} />); submit();
  await screen.findByText("已找回保存的分析；本次查询未调用 AI。");
  expect(complete).toHaveBeenCalledOnce();
  submit(); await waitFor(() => expect(fetcher.mock.calls.filter(call => call[0].includes("/requests/"))).toHaveLength(2));
  expect(fetcher.mock.calls.filter(call => call[1]?.method === "POST")).toHaveLength(1);
});

test("reload queries a saved attempt without POST and unknown status requires explicit new attempt", async () => {
  const old = crypto.randomUUID();
  localStorage.setItem(`lw-relation-attempt-v1:workspace:${old}`, JSON.stringify({ request_id: old, question: "Recovered question", document_ids: ids, force_refresh: false, created_at: "2026-10-23T00:00:00Z" }));
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/session/current")) return new Response(JSON.stringify({ csrf_token: "csrf" }));
    if (init?.method === "POST") { const body = JSON.parse(init.body as string); return new Response(JSON.stringify(done(body.request_id))); }
    return new Response(JSON.stringify({ request_id: old, status: "unknown", readonly: true }));
  }); vi.stubGlobal("fetch", fetcher);
  render(<Form initial="" />);
  await screen.findByText("原请求状态不明，只查询不会重新调用 AI。重启后也不会自动重发。");
  expect(screen.getByLabelText("AI 关系分析问题")).toHaveValue("Recovered question");
  submit(); await waitFor(() => expect(fetcher.mock.calls.filter(call => call[0].includes("/requests/"))).toHaveLength(2));
  expect(fetcher.mock.calls.some(call => call[1]?.method === "POST")).toBe(false);
  fireEvent.click(screen.getByRole("checkbox")); submit();
  await screen.findByText("本次分析已保存，可能已产生 API 费用。");
  const post = fetcher.mock.calls.find(call => call[1]?.method === "POST")!;
  const body = JSON.parse(post[1]!.body as string);
  expect(body.force_refresh).toBe(true); expect(body.request_id).not.toBe(old);
});

test("cached result is free, and unchecked repeats never create a new request", async () => {
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/session/current")) return new Response(JSON.stringify({ csrf_token: "csrf" }));
    if (init?.method === "POST") return new Response(JSON.stringify(done(JSON.parse(init.body as string).request_id, { cache_hit: true })));
    return new Response(JSON.stringify(done("saved", { readonly: true })));
  }); vi.stubGlobal("fetch", fetcher);
  render(<Form />); submit();
  await screen.findByText("复用已有分析；本次未新增 AI 请求。");
  submit(); await screen.findByText("已找回保存的分析；本次查询未调用 AI。");
  expect(fetcher.mock.calls.filter(call => call[1]?.method === "POST")).toHaveLength(1);
});

test("stopping wait keeps the request and a remount restores only by GET", async () => {
  let finish!: (value: Response) => void; let identifier = "";
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith("/session/current")) return Promise.resolve(new Response(JSON.stringify({ csrf_token: "csrf" })));
    if (init?.method === "POST") { identifier = JSON.parse(init.body as string).request_id; return new Promise<Response>(resolve => { finish = resolve; }); }
    return Promise.resolve(new Response(JSON.stringify({ request_id: identifier, status: "running" })));
  }); vi.stubGlobal("fetch", fetcher);
  const view = render(<Form />); submit(); await waitFor(() => expect(finish).toBeDefined());
  fireEvent.click(screen.getByRole("button", { name: "停止等待" }));
  await screen.findByText("原请求已登记，尚未确认完成。可关闭后回来查询，请勿重复发起。");
  view.unmount(); finish(new Response(JSON.stringify(done(identifier))));
  const complete = vi.fn(async () => {});
  fetcher.mockImplementation(async () => new Response(JSON.stringify(done(identifier, { readonly: true }))));
  render(<Form complete={complete} initial="" />);
  await screen.findByText("已找回保存的分析；本次查询未调用 AI。");
  expect(complete).toHaveBeenCalledOnce();
  expect(fetcher.mock.calls.filter(call => call[1]?.method === "POST")).toHaveLength(1);
});

test("a late status from another workspace cannot replace the new context", async () => {
  const old = crypto.randomUUID();
  localStorage.setItem(`lw-relation-attempt-v1:workspace:${old}`, JSON.stringify({ request_id: old, question: "Old question", document_ids: ids, force_refresh: false, created_at: "2026-10-23T00:00:00Z" }));
  let finish!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { finish = resolve; })));
  const complete = vi.fn(async () => {}), view = render(<Form complete={complete} />);
  await waitFor(() => expect(finish).toBeDefined());
  view.rerender(<Form workspace="different-workspace" complete={complete} />);
  finish(new Response(JSON.stringify(done(old))));
  await Promise.resolve(); expect(complete).not.toHaveBeenCalled();
  expect(screen.queryByText(/已找回保存的分析/)).not.toBeInTheDocument();
});


test("reversing paper order does not recover an attempt with different question semantics", async () => {
  const old = crypto.randomUUID();
  localStorage.setItem(`lw-relation-attempt-v1:workspace:${old}`, JSON.stringify({ request_id: old, question: "Does the first support the second?", document_ids: ids, force_refresh: false, created_at: "2026-10-23T00:00:00Z" }));
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
  render(<Form papers={["b", "a"]} initial="" />);
  await Promise.resolve();
  expect(fetcher).not.toHaveBeenCalled();
  expect(screen.getByLabelText("AI 关系分析问题")).toHaveValue("");
  expect(screen.queryByText(/状态不明/)).not.toBeInTheDocument();
});
