import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { LinkedPdfPane, type PaneLocation } from "./LinkedPdfPane";

vi.mock("../reader/PdfReader", () => ({ PdfReader: (props: unknown) => <div data-testid="linked-reader">{JSON.stringify(props)}</div> }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const source = { document_id: "a", display_name: "a.pdf", physical_page: 2, excerpt: "Exact saved source", content_id: "c".repeat(64), rects: [[10, 20, 300, 80] as const] };
const location: PaneLocation = { id: "a", page: 2, nonce: 1, evidence: source };

test("keeps the excerpt and highlights only the matching content snapshot", async () => {
  let content = "c".repeat(64);
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ document_id: "a", content_id: content, current_document_version_id: "v", filename: "a.pdf" })));
  vi.stubGlobal("fetch", fetcher);
  const ready = vi.fn();
  const { rerender } = render(<LinkedPdfPane side="left" location={location} onReady={ready} onProgress={vi.fn()} />);
  await screen.findByText("证据定位 · 第 2 页 · 版本一致");
  expect(JSON.parse((await screen.findByTestId("linked-reader")).textContent!).citationHighlight.rects).toEqual(source.rects);
  content = "d".repeat(64);
  rerender(<LinkedPdfPane side="left" location={{ ...location, nonce: 2 }} onReady={ready} onProgress={vi.fn()} />);
  await screen.findByText("证据定位 · 第 2 页 · 来源已变化");
  expect(screen.getByText("Exact saved source")).toBeVisible();
  expect(JSON.parse(screen.getByTestId("linked-reader").textContent!).citationHighlight).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(2);
});

test("ignores an old metadata response when switching documents", async () => {
  let resolveOld!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/a") ? new Promise<Response>(resolve => { resolveOld = resolve; })
    : Promise.resolve(new Response(JSON.stringify({ document_id: "b", content_id: "d".repeat(64), current_document_version_id: "v2", filename: "b.pdf" })))));
  const ready = vi.fn(), progress = vi.fn();
  const { rerender } = render(<LinkedPdfPane side="right" location={location} onReady={ready} onProgress={progress} />);
  rerender(<LinkedPdfPane side="right" location={{ id: "b", page: 1, nonce: 2 }} onReady={ready} onProgress={progress} />);
  await screen.findByTestId("linked-reader");
  resolveOld(new Response(JSON.stringify({ document_id: "a", content_id: source.content_id, current_document_version_id: "v", filename: "a.pdf" })));
  await waitFor(() => expect(screen.getByTestId("linked-reader")).toHaveTextContent('"documentId":"b"'));
  expect(screen.queryByText("Exact saved source")).toBeNull();
  expect(ready.mock.calls.filter(call => call[1]).every(call => call[1].documentId === "b")).toBe(true);
});
