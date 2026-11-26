import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useState } from "react";
import { QuickOpen } from "./QuickOpen";
import { documentMatches, findDocuments } from "./documentSearch";
import type { DocumentListItem } from "./documents";

afterEach(() => { cleanup(); vi.clearAllMocks(); });
const docs: DocumentListItem[] = [
  { document_id: "a", filename: "Camera-trap study.pdf", relative_folder: "Écologie/Methods", library_name: "Field work", authorization_source: "test" },
  { document_id: "b", filename: "Genotyping.pdf", relative_folder: "实验/微卫星", authorization_source: "test" },
  { document_id: "c", filename: "Animal movements.pdf", relative_folder: "Other", authorization_source: "test" },
];
const props = { documents: docs, records: {}, openIds: [], selected: [], commands: [{ id: "notes", title: "研究记录", description: "已保存的问答和笔记", run: vi.fn() }],
  onClose: vi.fn(), onOpen: vi.fn(), onCache: vi.fn(), onSelect: vi.fn(), onCompare: vi.fn(), onSplit: vi.fn(), documentApi: { listDocuments: vi.fn(async () => docs) } };

it("matches independent words across filenames, folders and tags, with stable exact-name priority", () => {
  const records = { b: { page: 4, totalPages: 9, favorite: true, lastOpened: 10, tags: ["DNA方法"] } };
  expect(documentMatches(docs[0]!, {}, "ecologie camera methods")).toBe(true);
  expect(documentMatches(docs[1]!, records, "DNA 微卫星")).toBe(true);
  expect(documentMatches(docs[0]!, {}, "camera missing")).toBe(false);
  expect(findDocuments(docs, records, "Genotyping", ["a"])[0]?.document_id).toBe("b");
  expect(findDocuments(docs, records, "", ["a"])[0]?.document_id).toBe("a");
});

it("focuses search, opens the keyboard-selected paper, and Shift+Enter opens its cache only", async () => {
  render(<QuickOpen {...props} />);
  const input = screen.getByRole("combobox", { name: "快速查找" });
  expect(input).toHaveFocus();
  fireEvent.change(input, { target: { value: "genotyping" } });
  await screen.findByRole("option", { name: /Genotyping.pdf/ });
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(props.onCache).toHaveBeenCalledWith("b");
  expect(props.onOpen).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: "Enter" });
  expect(props.onOpen).toHaveBeenCalledWith("b");
});

it("never treats IME confirmation as an open command", () => {
  const open = vi.fn();
  render(<QuickOpen {...props} onOpen={open} />);
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter", isComposing: true, keyCode: 229 });
  expect(open).not.toHaveBeenCalled();
});

it("does not invent an empty library on fetch failure and can retry", async () => {
  const api = { listDocuments: vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(docs) };
  render(<QuickOpen {...props} documents={[]} documentApi={api} />);
  await screen.findByRole("alert");
  expect(screen.getByText("列表尚未成功读取，请刷新重试。")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "刷新列表" }));
  await screen.findByRole("option", { name: /Genotyping.pdf/ });
});

it("keeps highlighted document identity when a refreshed list changes ordering", async () => {
  let finish!: (items: DocumentListItem[]) => void;
  const api = { listDocuments: vi.fn(async () => new Promise<DocumentListItem[]>(resolve => { finish = resolve; })) };
  render(<QuickOpen {...props} documentApi={api} />);
  const input = screen.getByRole("combobox");
  fireEvent.keyDown(input, { key: "ArrowDown" }); // camera, after animal
  expect(document.getElementById(input.getAttribute("aria-activedescendant")!)).toHaveTextContent("Camera-trap");
  await act(async () => finish([{ ...docs[0]!, document_id: "first", filename: "AAA.pdf" }, ...docs]));
  expect(document.getElementById(input.getAttribute("aria-activedescendant")!)).toHaveTextContent("Camera-trap");
});

it("limits initial DOM results for a large library but keyboard can continue into the next page", async () => {
  const many = Array.from({ length: 1000 }, (_, i) => ({ ...docs[0]!, document_id: String(i), filename: `Paper ${String(i).padStart(4, "0")}.pdf` }));
  render(<QuickOpen {...props} documents={many} documentApi={{ listDocuments: async () => many }} />);
  expect(screen.getAllByRole("option")).toHaveLength(60);
  const input = screen.getByRole("combobox");
  for (let i = 0; i < 60; i++) fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(document.getElementById(input.getAttribute("aria-activedescendant")!)).toHaveTextContent("Paper 0060.pdf");
  expect(screen.getAllByRole("option")).toHaveLength(61);
});

it("keeps multiselect explicit, blocks unavailable scope, and enforces the eight-paper cap", async () => {
  const many = Array.from({ length: 9 }, (_, i) => ({ ...docs[0]!, document_id: String(i), filename: `Paper ${i}.pdf` }));
  function Harness() { const [selected, select] = useState(many.slice(0, 8).map(d => d.document_id)); return <QuickOpen {...props} documents={many} documentApi={{ listDocuments: async () => many }} selected={selected} onSelect={select} />; }
  const view = render(<Harness />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "Paper 8" } });
  fireEvent.click(screen.getByRole("button", { name: "加入对比" }));
  expect(await screen.findByText("一次最多选择 8 篇，请先移出一篇。")).toBeVisible();
  expect(screen.getByText("已选 8 / 8 篇")).toBeVisible();
  view.unmount();
  render(<QuickOpen {...props} selected={["a", "removed"]} />);
  expect(screen.getByRole("button", { name: "研读所选文献" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "并排前两篇" })).toBeDisabled();
});

it("finds a function and runs only the explicit command", async () => {
  const run = vi.fn(), close = vi.fn();
  render(<QuickOpen {...props} onClose={close} commands={[{ id: "notes", title: "研究记录", description: "已保存的笔记", run }]} />);
  fireEvent.click(screen.getByRole("button", { name: "找功能" }));
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "笔记" } });
  expect(run).not.toHaveBeenCalled();
  fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
  await waitFor(() => expect(run).toHaveBeenCalledOnce());
  expect(close).toHaveBeenCalledOnce();
});
