import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { StudyHistory } from "./StudyHistory";
import { downloadText } from "../ui/exportText";

vi.mock("../ui/exportText", async importOriginal => ({ ...await importOriginal<typeof import("../ui/exportText")>(), downloadText: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks(); });
const entry = { cache_key: "cache", question: "Compare A and B", created_at: "2026-10-23T00:00:00Z", model: "test", context_kind: "full_text", current: false, invalid: false,
  documents: [{ document_id: "a", display_name: "A.pdf", text_status: "current", read_mode: "full_text" }, { document_id: "b", display_name: "B.pdf", text_status: "changed", read_mode: "full_text" }],
  claims: [{ conclusion: "Group conclusion", evidence: [{ document_id: "b", display_name: "B.pdf", physical_page: 2, excerpt: "Older source evidence.", verified: false, content_id: null, rects: [] }] }], limitations: ["Pending verification."] };
const props = { documentId: "a", onClose: vi.fn(), onJump: vi.fn(), onReread: vi.fn(), onFindNotes: vi.fn() };
function setup(invalid = false) {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ display_name: "A.pdf", total: 1, has_more: false, items: [{ ...entry, invalid }] })));
  vi.stubGlobal("fetch", fetcher);
  render(<StudyHistory {...props} />);
  return fetcher;
}

it("copies and exports historical group analysis without API requests or lost verification state", async () => {
  const copy = vi.fn().mockResolvedValue(undefined); vi.stubGlobal("navigator", { clipboard: { writeText: copy } });
  const fetcher = setup();
  fireEvent.click(await screen.findByRole("button", { name: "复制分析与依据" }));
  await screen.findByText("已复制分析与原文依据");
  expect(copy.mock.calls[0]![0]).toContain("历史分析"); expect(copy.mock.calls[0]![0]).toContain("Older source evidence.");
  fireEvent.click(screen.getByRole("button", { name: "导出 Markdown" }));
  expect(vi.mocked(downloadText).mock.calls[0]![1]).toContain("B.pdf");
  expect(vi.mocked(downloadText).mock.calls[0]![1]).toContain("历史引句，当前未核实");
  fireEvent.click(screen.getByRole("button", { name: "导出记录（JSON）" }));
  const json = JSON.parse(vi.mocked(downloadText).mock.calls[1]![1]);
  expect(json.study).toEqual(entry); expect(json.viewed_at).toBeTruthy();
  expect(vi.mocked(downloadText).mock.calls[1]![2]).toBe("application/json;charset=utf-8");
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("opens an explicit research-record search without pretending it is the same analysis", async () => {
  const fetcher = setup();
  fireEvent.click(await screen.findByRole("button", { name: "在研究记录中查找" }));
  expect(props.onFindNotes).toHaveBeenCalledWith(entry.question);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("offers a file export when clipboard access fails", async () => {
  vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
  setup(); fireEvent.click(await screen.findByRole("button", { name: "复制分析与依据" }));
  await screen.findByText("复制未成功，请使用导出保存。");
  expect(screen.getByRole("button", { name: "导出 Markdown" })).toBeEnabled();
});

it("does not offer invalid analysis as a verified export", async () => {
  setup(true); await screen.findByText(/缓存格式或依据核验失败/);
  expect(screen.queryByRole("button", { name: "复制分析与依据" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "导出 Markdown" })).not.toBeInTheDocument();
});
