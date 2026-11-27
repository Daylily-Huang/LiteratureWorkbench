import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ResearchNotebook, notebookMarkdown, type ResearchEntry } from "./ResearchNotebook";
import { readNotebookDrafts, writeNotebookDraft, type NotebookDraft } from "./notebookDrafts";

afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const workspace = "a".repeat(64);
const entry: ResearchEntry = { id: "saved", title: "比较方法", question: "Which methods?", answer: { answer: "Original answer", scope: "selected", provider: "deepseek", citations: [{ document_id: "d1", display_name: "paper.pdf", physical_page: 3, excerpt: "Source evidence", rects: [], source_changed: true, content_id: "abc" }] }, note: "", starred: false, revision: 2, document_ids: ["d1"], created_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:00:00Z" };
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function server(options: { conflict?: boolean; loseAck?: boolean; pageTwo?: boolean; failWrite?: boolean } = {}) {
  const records = new Map([[entry.id, structuredClone(entry)]]);
  const fetcher = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("session/current")) return response({ csrf_token: "csrf" });
    if (url.includes("notebook/index?")) {
      const params = new URL(url, "http://localhost").searchParams;
      const items = options.pageTwo && params.get("offset") !== "30" ? [] : [...records.values()];
      return response({ items: items.map(({ id, title, question, starred, revision, created_at, updated_at }) => ({ id, title, question, starred, revision, created_at, updated_at })), total: options.pageTwo ? 31 : records.size, has_more: options.pageTwo && params.get("offset") !== "30", workspace_id: workspace });
    }
    const id = url.split("/").at(-1)!;
    if (init?.method === "PATCH") {
      if (options.failWrite) throw new TypeError("offline");
      if (options.conflict) return response({ code: "note_conflict" }, 409);
      const patch = JSON.parse(init.body as string), old = records.get(id)!;
      if (patch.revision !== old.revision) return response({ code: "note_conflict" }, 409);
      records.set(id, { ...old, ...patch, revision: old.revision + 1 });
      if (options.loseAck) throw new TypeError("ack lost after commit");
      return response({ revision: old.revision + 1 });
    }
    if (init?.method === "POST") {
      if (options.failWrite) throw new TypeError("offline");
      const value = JSON.parse(init.body as string);
      const saved = { ...entry, id: value.note_id, title: value.title, note: value.note, starred: value.starred ?? true, question: "", answer: {}, revision: 0 };
      records.set(saved.id, saved); return response(saved, 201);
    }
    return records.has(id) ? response(records.get(id)) : response({ code: "note_not_found" }, 404);
  });
  vi.stubGlobal("fetch", fetcher);
  return { fetcher, records, options };
}
const patchCalls = (fetcher: ReturnType<typeof server>["fetcher"]) => fetcher.mock.calls.filter(c => c[1]?.method === "PATCH");
function draft(overrides: Partial<NotebookDraft> = {}): NotebookDraft {
  return { version: 1, key: "draft-one", entryId: entry.id, title: entry.title, note: "Offline thought", starred: false, revision: 2, isNew: false, savedAt: "2026-10-23T00:00:00Z", ...overrides };
}

test("keeps original answer immutable and retains conflict draft when closing", async () => {
  const { fetcher } = server({ conflict: true }); const close = vi.fn();
  render(<ResearchNotebook onClose={close} onJump={vi.fn()} initialEntryId="saved" />);
  await screen.findByText("Original answer");
  fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "My interpretation" } });
  fireEvent.click(screen.getByRole("button", { name: "保存笔记" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("其他窗口修改");
  expect(screen.getByLabelText("我的思考")).toHaveValue("My interpretation");
  expect(JSON.parse(patchCalls(fetcher)[0]![1]!.body as string)).toEqual({ title: "比较方法", note: "My interpretation", starred: false, revision: 2 });
  fireEvent.click(screen.getByRole("button", { name: "关闭研究记录" }));
  expect(close).toHaveBeenCalledOnce();
  expect(readNotebookDrafts(workspace).some(d => d.note === "My interpretation")).toBe(true);
});

test("warns before changed sources and exports snapshot, personal note and copied provenance", async () => {
  server(); const jump = vi.fn();
  render(<ResearchNotebook onClose={vi.fn()} onJump={jump} initialEntryId="saved" />);
  await screen.findByText("Original answer"); fireEvent.click(screen.getByText("查看 AI 原始回答与原文依据")); fireEvent.click(screen.getByText(/paper.pdf · 第 3 页/));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(screen.getByRole("button", { name: "回到原文第 3 页" })); expect(jump).not.toHaveBeenCalled();
  confirm.mockReturnValue(true); fireEvent.click(screen.getByRole("button", { name: "回到原文第 3 页" }));
  await waitFor(() => expect(jump).toHaveBeenCalled());
  const markdown = notebookMarkdown({ ...entry, answer: { ...entry.answer, copied_from_entry_id: "original-id" }, note: "My thoughts" });
  for (const text of ["Original answer", "Source evidence", "abc", "文件已变化", "My thoughts", "所选 1 篇", "original-id"]) expect(markdown).toContain(text);
});

test("automatically saves after typing and acknowledges a lost response without repeating the write", async () => {
  const { fetcher, records } = server({ loseAck: true });
  render(<ResearchNotebook onClose={vi.fn()} onJump={vi.fn()} initialEntryId="saved" />);
  await screen.findByText("Original answer");
  fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "Auto saved reasoning" } });
  await screen.findByText("已自动保存到研究记录", {}, { timeout: 2500 });
  expect(records.get("saved")?.note).toBe("Auto saved reasoning");
  expect(patchCalls(fetcher)).toHaveLength(1); expect(readNotebookDrafts(workspace)).toHaveLength(0);
});

test("retains newer typing while an earlier save is pending and uses the acknowledged next revision", async () => {
  const { fetcher } = server(); let complete!: (response: Response) => void;
  const original = fetcher.getMockImplementation()!;
  let held = false;
  fetcher.mockImplementation(async (url, init) => {
    if (init?.method === "PATCH" && !held) { held = true; return await new Promise<Response>(resolve => { complete = resolve; }); }
    return original(url, init);
  });
  render(<ResearchNotebook onClose={vi.fn()} onJump={vi.fn()} initialEntryId="saved" />);
  await screen.findByText("Original answer");
  fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "First" } }); fireEvent.click(screen.getByRole("button", { name: "保存笔记" }));
  await waitFor(() => expect(complete).toBeDefined());
  fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "Second not yet saved" } });
  complete(response({ revision: 3 })); await screen.findByText(/此前修改已保存/);
  expect(screen.getByLabelText("我的思考")).toHaveValue("Second not yet saved");
  expect(readNotebookDrafts(workspace).some(d => d.note === "Second not yet saved" && d.revision === 3)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "保存笔记" }));
  await waitFor(() => expect(patchCalls(fetcher)).toHaveLength(2));
  expect(JSON.parse(patchCalls(fetcher)[1]![1]!.body as string).revision).toBe(3);
});

test("loads list summaries and deep-linked details independently of the list page", async () => {
  const { fetcher } = server({ pageTwo: true });
  render(<ResearchNotebook onClose={vi.fn()} onJump={vi.fn()} initialEntryId="saved" />);
  await screen.findByText("Original answer");
  expect(fetcher.mock.calls.some(c => c[0] === "/api/workspace/notebook")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "下一页记录" }));
  await waitFor(() => expect(fetcher.mock.calls.some(c => String(c[0]).includes("offset=30"))).toBe(true));
  expect(screen.getByLabelText("我的思考")).toHaveValue("");
});

test("restores an offline new-note draft with its original identity and saves it once", async () => {
  const { fetcher, records } = server();
  writeNotebookDraft(workspace, draft({ entryId: "new-draft", title: "New field study", isNew: true, revision: 0 }));
  render(<ResearchNotebook onClose={vi.fn()} onJump={vi.fn()} />);
  await screen.findByText(/本机待同步草稿/); fireEvent.click(screen.getByText(/本机待同步草稿/));
  fireEvent.click(screen.getByRole("button", { name: "恢复这份草稿" })); await screen.findByLabelText("我的思考");
  fireEvent.click(screen.getByRole("button", { name: "保存笔记" })); await screen.findByText("已自动保存到研究记录");
  expect(records.get("new-draft")?.note).toBe("Offline thought");
  expect(fetcher.mock.calls.filter(c => c[1]?.method === "POST")).toHaveLength(1);
});

test("a conflicted restored draft keeps its old revision across closes and never auto-overwrites", async () => {
  const { fetcher, records } = server(); records.set("saved", { ...entry, revision: 4, note: "Other window's newer note" });
  writeNotebookDraft(workspace, draft());
  render(<ResearchNotebook onClose={vi.fn()} onJump={vi.fn()} />);
  fireEvent.click(await screen.findByText(/本机待同步草稿/)); fireEvent.click(screen.getByRole("button", { name: "恢复这份草稿" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("其他窗口修改");
  fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "Continued old thinking" } });
  fireEvent.click(screen.getByRole("button", { name: "关闭研究记录" }));
  expect(readNotebookDrafts(workspace).filter(d => d.note === "Continued old thinking").every(d => d.revision === 2)).toBe(true);
  expect(patchCalls(fetcher)).toHaveLength(0);
});

test("retains unsent text on network failure and resumes when online", async () => {
  const { options, records } = server({ failWrite: true });
  render(<ResearchNotebook onClose={vi.fn()} onJump={vi.fn()} initialEntryId="saved" />);
  await screen.findByText("Original answer"); fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "Offline note" } });
  fireEvent.click(screen.getByRole("button", { name: "保存笔记" })); expect(await screen.findByRole("alert")).toHaveTextContent("暂未同步");
  expect(readNotebookDrafts(workspace).some(d => d.note === "Offline note")).toBe(true);
  options.failWrite = false; window.dispatchEvent(new Event("online"));
  await screen.findByText("已自动保存到研究记录"); expect(records.get("saved")?.note).toBe("Offline note");
});

test("does not discard typed text when browser draft storage fails", async () => {
  server(); const close = vi.fn();
  render(<ResearchNotebook onClose={close} onJump={vi.fn()} initialEntryId="saved" />);
  await screen.findByText("Original answer");
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("quota", "QuotaExceededError"); });
  vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "Still in the editor" } });
  expect(await screen.findByRole("alert")).toHaveTextContent("浏览器草稿存储不可用");
  fireEvent.click(screen.getByRole("button", { name: "关闭研究记录" }));
  expect(close).not.toHaveBeenCalled(); expect(screen.getByLabelText("我的思考")).toHaveValue("Still in the editor");
});

test("retains an existing conflicting copy and uses a fresh identity only on an explicit next copy", async () => {
  const { fetcher } = server({ conflict: true }); const original = fetcher.getMockImplementation()!;
  const ids: string[] = [];
  fetcher.mockImplementation(async (url, init) => {
    if (String(url).endsWith("/copy")) {
      const body = JSON.parse(init!.body as string); ids.push(body.note_id);
      return ids.length === 1 ? response({ code: "note_conflict" }, 409) : response({ ...entry, id: body.note_id, title: body.title, note: body.note });
    }
    return original(url, init);
  });
  render(<ResearchNotebook onClose={vi.fn()} onJump={vi.fn()} initialEntryId="saved" />);
  await screen.findByText("Original answer"); fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "My local interpretation" } });
  fireEvent.click(screen.getByRole("button", { name: "保存笔记" })); await screen.findByText(/这条记录已在其他窗口修改/);
  fireEvent.click(screen.getByRole("button", { name: "另存为新笔记" })); await screen.findByText(/之前的副本已存在且内容不同/);
  fireEvent.click(screen.getByRole("button", { name: "另存为新笔记" })); await screen.findByText(/已另存为独立笔记/);
  expect(ids).toHaveLength(2); expect(ids[0]).not.toBe(ids[1]);
  expect(screen.getByLabelText("我的思考")).toHaveValue("My local interpretation");
});

test("a delayed server-version reload cannot replace text typed after the reload began", async () => {
  const { fetcher } = server({ conflict: true }); const original = fetcher.getMockImplementation()!;
  render(<ResearchNotebook onClose={vi.fn()} onJump={vi.fn()} initialEntryId="saved" />);
  await screen.findByText("Original answer"); fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "Conflict draft" } });
  fireEvent.click(screen.getByRole("button", { name: "保存笔记" })); await screen.findByText(/这条记录已在其他窗口修改/);
  let complete!: (value: Response) => void;
  fetcher.mockImplementation(async (url, init) => String(url).endsWith("/notebook/saved") && (!init?.method || init.method === "GET") ? await new Promise<Response>(resolve => { complete = resolve; }) : original(url, init));
  vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "载入服务器版本" })); await waitFor(() => expect(complete).toBeDefined());
  fireEvent.change(screen.getByLabelText("我的思考"), { target: { value: "New thought during reload" } });
  complete(response({ ...entry, note: "Other window", revision: 8 }));
  await screen.findByText(/读取期间又有新输入/);
  expect(screen.getByLabelText("我的思考")).toHaveValue("New thought during reload");
  expect(readNotebookDrafts(workspace).some(d => d.note === "New thought during reload" && d.revision === 2)).toBe(true);
});
