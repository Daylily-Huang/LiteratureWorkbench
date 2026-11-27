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
  pdfQuads: [],
  buttonAnchor: { x: 0, y: 0 },
  viewportAnchor: { x: 100, y: 100 },
  translationEligible: true,
};


describe("TranslationPopover", () => {
  it("resizes and resets without submitting the translation again", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1200 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 900 });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return { x:110,y:76,left:110,top:76,right:510,bottom:376,width:Number.parseFloat(this.style.width)||400,height:Number.parseFloat(this.style.height)||300,toJSON:()=>({}) };
    });
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({csrf_token:"test"})))
      .mockResolvedValueOnce(new Response(JSON.stringify({id:"task-resize",status:"completed",translated_text:"译文"})));
    render(<TranslationPopover selected={selected} fetcher={fetcher} />);
    await screen.findByText("译文");
    fireEvent.keyDown(screen.getByRole("button", {name:"调整翻译框大小"}), {key:"ArrowRight"});
    expect(screen.getByRole("region", {name:"选区翻译"})).toHaveStyle({width:"420px"});
    fireEvent.keyDown(screen.getByRole("button", {name:"调整翻译框大小"}), {key:"ArrowDown"});
    expect(screen.getByRole("region", {name:"选区翻译"})).toHaveStyle({width:"420px",height:"320px"});
    fireEvent.click(screen.getByRole("button", {name:"重置大小"}));
    expect(screen.getByRole("region", {name:"选区翻译"}).style.width).toBe("");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
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

  it.each([
    ["selection_version_stale", "文档已更新，请重新选择后再翻译"],
    ["selection_parse_pending", "文档尚未完成解析，暂时无法翻译选区"],
    ["selection_text_mismatch", "选中文本未能与页面文字对齐，请缩小范围重新选择"],
  ])("maps %s to an actionable message", async (code, message) => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ code }), { status: 409 }));
    render(<TranslationPopover fetcher={fetcher} selected={selected} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });

  it("retries an expired selection with the latest annotation identity", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf-a" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        code: "selection_not_current",
      }), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf-b" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: "task-b", status: "completed", translated_text: "新译文",
        provider: "deepseek", model: "deepseek-chat",
      }), { status: 200 }));
    const { rerender } = render(<TranslationPopover fetcher={fetcher} selected={selected} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("选区已过期");

    rerender(<TranslationPopover
      fetcher={fetcher}
      selected={{ ...selected, documentVersionId: "version-b", contentId: "content-b" }}
    />);
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(fetcher).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole("button", { name: "重试翻译" }));
    await waitFor(() => expect(screen.getByText("新译文")).toBeVisible());
    const retryBody = JSON.parse(String((fetcher.mock.calls[3]?.[1] as RequestInit).body));
    expect(retryBody).toMatchObject({
      document_version_id: "version-b",
      content_id: "content-b",
    });
  });

  it("drags by the header and clamps the card inside the viewport", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 800 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 600 });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 110, y: 76, left: 110, top: 76, right: 470, bottom: 276,
      width: 360, height: 200, toJSON: () => ({}),
    });
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: "task-a", status: "completed", translated_text: "译文",
        provider: "deepseek", model: "deepseek-chat",
      }), { status: 200 }));
    render(<TranslationPopover fetcher={fetcher} selected={selected} />);
    const card = screen.getByRole("region", { name: "选区翻译" });
    const handle = screen.getByText("选区翻译").parentElement;
    if (!(handle instanceof HTMLElement)) throw new Error("drag handle missing");
    await waitFor(() => expect(card).toHaveStyle({ left: "110px", top: "76px" }));

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 140, clientY: 100 });
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 1000, clientY: 1000 });
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 1000, clientY: 1000 });

    expect(card).toHaveStyle({ left: "428px", top: "388px" });
  });
});


it("labels a covering cache as a reference and charges only after an explicit exact request", async () => {
  const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (!init?.body) return new Response(JSON.stringify({ csrf_token: "test" }));
    const exact = JSON.parse(String(init.body)).prefer_exact;
    return new Response(JSON.stringify({ id: "cache", status: "completed", provider: "deepseek",
      translated_text: exact ? "精确译文" : "整段译文", cache_hit: !exact,
      reuse: { mode: exact ? "fresh" : "reference", cloud_requested: exact, source_text: "Full paragraph containing selected text." },
    }));
  });
  render(<TranslationPopover selected={selected} fetcher={fetcher} />);
  await screen.findByText("整段缓存参考");
  expect(screen.getByRole("status")).toHaveAttribute("title", "本次未调用云端 API，不新增 API 费用。");
  expect(screen.getByText("Full paragraph containing selected text.")).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "仅翻译当前选区（可能产生 API 费用）" }));
  await screen.findByText("精确译文");
  expect(screen.getByRole("status")).toHaveAttribute("title", "本次已请求云端 API，可能产生费用。");
  expect(fetcher).toHaveBeenCalledTimes(4);
  cleanup();
});

it("does not label fallback cache as free when cloud was already attempted", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({csrf_token:"test"})))
    .mockResolvedValueOnce(new Response(JSON.stringify({ id:"fallback", status:"completed", provider:"qwen", translated_text:"本地缓存",
      reuse:{mode:"exact",cloud_requested:true}, cache_hit:true })));
  render(<TranslationPopover selected={selected} fetcher={fetcher} />);
  await screen.findByText("本地缓存");
  expect(screen.getByRole("status")).toHaveAttribute("title", "本次已请求云端 API，可能产生费用。");
  expect(screen.getByRole("status")).toHaveTextContent("云端请求");
  expect(screen.queryByText("本次未调用云端 API，不新增 API 费用。")).not.toBeInTheDocument();
  cleanup();
});
