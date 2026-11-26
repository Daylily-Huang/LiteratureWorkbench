import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { EvidenceMatrix, type EvidenceRow } from "./EvidenceMatrix";
import { downloadBlob } from "../ui/exportText";
vi.mock("../ui/exportText", async original => ({ ...await original<typeof import("../ui/exportText")>(), downloadBlob: vi.fn() }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.mocked(downloadBlob).mockClear(); });
const documents = ["a", "b"].map(id => ({ document_id: id, filename: id + ".pdf", relative_folder: "", authorization_source: "test" }));
const citation = { document_id: "a", display_name: "a.pdf", physical_page: 4, excerpt: "Saved exact evidence", rects: [[10, 20, 300, 80] as const], content_id: "c".repeat(64), source_changed: true };
const row: EvidenceRow = { id: "note", title: "Q", question: "Compare?", note: "My view", document_ids: ["a", "b", "outside"], created_at: "2026-10-22T00:00:00Z", updated_at: "", scope: "selected", provider: "controlled", model: "test", citations: [citation] };
function page(items = [row], total = items.length, offset = 0) { return { items, total, offset, has_more: offset + items.length < total, viewed_at: "now" }; }
function props() { return { documents, onOpen: vi.fn(), onNotebook: vi.fn(), onAsk: vi.fn() }; }
function stub(value = page()) { vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value)))); }

test("keeps missing coverage explicit and warns before navigating changed source", async () => {
  stub(); const callbacks = props();
  render(<EvidenceMatrix {...callbacks} />);
  expect(await screen.findByText("Saved exact evidence")).toBeVisible();
  expect(screen.getByText("本次回答未引用")).toBeVisible();
  expect(screen.getByText(/原提问还包含组外文献/)).toBeVisible();
  vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(screen.getByRole("button", { name: "右栏 · 第 4 页" })); expect(callbacks.onOpen).not.toHaveBeenCalled();
  vi.mocked(window.confirm).mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "右栏 · 第 4 页" })); expect(callbacks.onOpen).toHaveBeenCalledWith("a", "right", 4);
  fireEvent.click(screen.getByRole("button", { name: "Compare?" })); expect(callbacks.onNotebook).toHaveBeenCalledWith("note");
});

test("passes complete citation context and prepares comparison without requesting AI", async () => {
  const current = { ...citation, source_changed: false }; stub(page([{ ...row, citations: [current] }]));
  const callbacks = props(), evidence = vi.fn();
  render(<EvidenceMatrix {...callbacks} onEvidence={evidence} />);
  fireEvent.click(await screen.findByRole("button", { name: "右栏 · 第 4 页" }));
  expect(evidence).toHaveBeenCalledWith(current, "right");
  expect(screen.getByText(/引用覆盖 1 \/ 2 篇/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "方法与样本" }));
  expect(callbacks.onAsk).toHaveBeenCalledWith(expect.stringContaining("不补猜数值"));
  expect(vi.mocked(fetch).mock.calls).toHaveLength(1);
});

test("pages through all scoped history and submits full-text searches from page one", async () => {
  const fetcher = vi.fn(async (url: string) => {
    const params = new URL(url, "http://localhost").searchParams;
    const offset = Number(params.get("offset")), query = params.get("q");
    return new Response(JSON.stringify(query ? page([{ ...row, question: "Old quote match" }]) : page([{ ...row, question: `Page ${offset}` }], 43, offset)));
  }); vi.stubGlobal("fetch", fetcher);
  render(<EvidenceMatrix {...props()} />);
  await screen.findByText("Page 0"); fireEvent.click(screen.getByRole("button", { name: "下一页证据" }));
  await screen.findByText("Page 20"); expect(screen.queryByText("Page 0")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("搜索证据问题"), { target: { value: "Straße 100%_" } });
  fireEvent.click(screen.getByRole("button", { name: "搜索证据" }));
  await screen.findByText("Old quote match");
  const params = new URL(fetcher.mock.calls.at(-1)![0], "http://localhost").searchParams;
  expect(params.getAll("document_ids")).toEqual(["a", "b"]); expect(params.get("offset")).toBe("0"); expect(params.get("q")).toBe("Straße 100%_");
  expect(screen.getByRole("button", { name: "下一页证据" })).toBeDisabled();
});

test("ignores old scope responses and resets paging when the selected papers change", async () => {
  const pending: ((value: Response) => void)[] = [];
  const fetcher = vi.fn((_url: string, _init?: RequestInit) => new Promise<Response>(resolve => pending.push(resolve))); vi.stubGlobal("fetch", fetcher);
  const callbacks = props(); const view = render(<EvidenceMatrix {...callbacks} />);
  view.rerender(<EvidenceMatrix {...callbacks} documents={[documents[1]!]} />);
  pending[1]!(new Response(JSON.stringify(page([{ ...row, question: "Current scope" }]))));
  await screen.findByText("Current scope");
  pending[0]!(new Response(JSON.stringify(page([{ ...row, question: "Late old scope" }]))));
  await waitFor(() => expect(fetcher.mock.calls[0]![1]?.signal?.aborted).toBe(true));
  expect(screen.queryByText("Late old scope")).not.toBeInTheDocument();
  expect(screen.getByText("Current scope")).toBeVisible();
});

test("read errors retry without displaying an empty success and empty groups make no requests", async () => {
  const fetcher = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(new Response(JSON.stringify(page()))); vi.stubGlobal("fetch", fetcher);
  const callbacks = props(); const view = render(<EvidenceMatrix {...callbacks} documents={[]} />);
  expect(fetcher).not.toHaveBeenCalled();
  view.rerender(<EvidenceMatrix {...callbacks} />);
  await screen.findByRole("alert"); expect(screen.queryByText("把比较建立在具体证据上")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试读取证据" }));
  await screen.findByText("Saved exact evidence");
});

test("exports all matching results independently of the visible page and can cancel late downloads", async () => {
  let complete!: (response: Response) => void;
  const fetcher = vi.fn((url: string, _init?: RequestInit) => url.includes("/export?") ? new Promise<Response>(resolve => { complete = resolve; }) : Promise.resolve(new Response(JSON.stringify(page([row], 43))))); vi.stubGlobal("fetch", fetcher);
  render(<EvidenceMatrix {...props()} />);
  await screen.findByText("Saved exact evidence");
  fireEvent.click(screen.getByRole("button", { name: "导出全部匹配（JSON）" }));
  const url = new URL(fetcher.mock.calls.at(-1)![0], "http://localhost");
  expect(url.searchParams.has("offset")).toBe(false); expect(url.searchParams.getAll("document_ids")).toEqual(["a", "b"]);
  fireEvent.click(screen.getByRole("button", { name: "取消导出" }));
  complete(new Response('{"total":43}'));
  await screen.findByText("已取消导出。"); expect(downloadBlob).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "导出全部匹配（Markdown）" }));
  complete(new Response("# all 43 original answers and sources\n<!-- literature-workbench-evidence-export-complete -->\n"));
  await waitFor(() => expect(downloadBlob).toHaveBeenCalledOnce());
  expect(vi.mocked(downloadBlob).mock.calls[0]![0]).toBe("多文献证据对照.md");
});

test("export failure is retryable and leaving or changing scope cannot download an old export", async () => {
  let complete!: (response: Response) => void;
  const fetcher = vi.fn((url: string) => url.includes("/export?") ? new Promise<Response>(resolve => { complete = resolve; }) : Promise.resolve(new Response(JSON.stringify(page())))); vi.stubGlobal("fetch", fetcher);
  const callbacks = props(), view = render(<EvidenceMatrix {...callbacks} />);
  await screen.findByText("Saved exact evidence");
  fireEvent.click(screen.getByRole("button", { name: "导出全部匹配（JSON）" })); complete(new Response("offline", { status: 503 }));
  await screen.findByText(/导出未完成/);
  fireEvent.click(screen.getByRole("button", { name: "导出全部匹配（JSON）" }));
  view.rerender(<EvidenceMatrix {...callbacks} documents={[documents[1]!]} />);
  complete(new Response("old scope export")); await screen.findByText("Compare?");
  expect(downloadBlob).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "导出全部匹配（JSON）" })); view.unmount(); complete(new Response("late after close"));
  await Promise.resolve(); expect(downloadBlob).not.toHaveBeenCalled();
});

test("keeps unavailable paper evidence visible and refuses incomplete export files", async () => {
  const fetcher = vi.fn(async (url: string) => url.includes("/export?") ? new Response('{"entries":[') : new Response(JSON.stringify(page()))); vi.stubGlobal("fetch", fetcher);
  render(<EvidenceMatrix {...props()} documents={[{ ...documents[0]!, available: false }, documents[1]!]} />);
  expect(await screen.findByText("Saved exact evidence")).toBeVisible();
  expect(screen.getByText("原文暂不可用，保留历史证据")).toBeVisible();
  expect(screen.getByRole("button", { name: "右栏 · 第 4 页" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "导出全部匹配（JSON）" }));
  await screen.findByText(/导出未完成/); expect(downloadBlob).not.toHaveBeenCalled();
});
