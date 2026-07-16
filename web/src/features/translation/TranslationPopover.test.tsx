import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SelectedText } from "../reader/selection";
import { calculatePopoverPosition, TranslationPopover } from "./TranslationPopover";


const selected: SelectedText = {
  documentId: "document-a",
  documentVersionId: "version-a",
  contentId: "content-a",
  pdfPage: 2,
  text: "selected text",
  contextBefore: "untrusted before",
  contextAfter: "untrusted after",
  rects: [],
  buttonAnchor: { x: 0, y: 0 },
  viewportAnchor: { x: 100, y: 100 },
};


describe("TranslationPopover", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("prefers the right side, falls back left, and clamps inside the viewport", () => {
    expect(calculatePopoverPosition(
      { x: 100, y: 100 },
      { width: 360, height: 200 },
      { width: 1000, height: 800 },
    )).toEqual({ left: 110, top: 76 });
    expect(calculatePopoverPosition(
      { x: 950, y: 790 },
      { width: 360, height: 200 },
      { width: 1000, height: 800 },
    )).toEqual({ left: 580, top: 588 });
    expect(calculatePopoverPosition(
      { x: 100, y: 5 },
      { width: 360, height: 200 },
      { width: 1000, height: 800 },
    )).toEqual({ left: 110, top: 12 });
  });

  it("repositions the measured card when the viewport changes", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 800 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 600 });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, right: 360, bottom: 200,
      width: 360, height: 200, toJSON: () => ({}),
    });
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: "task-a", status: "completed", translated_text: "译文",
        provider: "deepseek", model: "deepseek-chat",
      }), { status: 200 }));
    const anchored = { ...selected, viewportAnchor: { x: 500, y: 300 } };

    render(<TranslationPopover fetcher={fetcher} selected={anchored} />);
    const card = screen.getByRole("region", { name: "选区翻译" });
    await waitFor(() => expect(card).toHaveStyle({ left: "130px", top: "276px" }));

    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1000 });
    fireEvent(window, new Event("resize"));

    await waitFor(() => expect(card).toHaveStyle({ left: "510px", top: "276px" }));
  });

  it("creates a bounded task, polls, and shows provider source", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "task-a", status: "queued", selected_text: "selected text" }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: "task-a", status: "completed", selected_text: "selected text",
        translated_text: "所选文字", source_language: "en", target_language: "zh-CN",
        provider: "deepseek", model: "deepseek-v4-flash", fallback_reason: null, error_code: null,
      })));

    render(<TranslationPopover fetcher={fetcher} pollInterval={1} selected={selected} />);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
    const body = JSON.parse(String((fetcher.mock.calls[1]?.[1] as RequestInit).body));
    expect(body).toMatchObject({
      document_id: "document-a", document_version_id: "version-a",
      content_id: "content-a", physical_page: 2, selected_text: "selected text",
      target_language: "zh-CN",
    });
    expect(body).not.toHaveProperty("context_before");
    expect(body).not.toHaveProperty("url");

    expect(screen.getByText("所选文字")).toBeVisible();
    expect(screen.getByText("DeepSeek · deepseek-v4-flash")).toBeVisible();
    expect(screen.getByRole("button", { name: "复制译文" })).toBeEnabled();
  });

  it("shows sanitized failure and retries with a new client task", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: "task-a", status: "failed", selected_text: "selected text",
        translated_text: null, provider: null, model: null,
        fallback_reason: null, error_code: "deepseek_key_unavailable",
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "task-b", status: "queued" }), { status: 202 }))
      .mockResolvedValue(new Response(JSON.stringify({ id: "task-b", status: "queued" })));
    render(<TranslationPopover fetcher={fetcher} selected={selected} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("DeepSeek 密钥未配置");
    fireEvent.click(screen.getByRole("button", { name: "重试翻译" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(4));
  });
});
