import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../../app/App";
import { PdfReader } from "./PdfReader";
import type { SelectedText } from "./selection";


const pdf = vi.hoisted(() => ({
  getDocument: vi.fn(),
  loadingDestroy: vi.fn(async () => undefined),
  renderCancels: [] as Array<ReturnType<typeof vi.fn>>,
  renderParameters: [] as Array<Record<string, unknown>>,
}));

async function renderedTextNode(page: HTMLElement): Promise<ChildNode> {
  await waitFor(() => {
    expect(page.querySelector(".textLayer")?.firstChild).toBeTruthy();
  });
  const node = page.querySelector(".textLayer")?.firstChild;
  if (node === null || node === undefined) throw new Error("mock text missing");
  return node;
}

function renderedPageNumbers(selector = "[data-page-number]"): string[] {
  return Array.from(document.querySelectorAll(selector), (node) =>
    node.getAttribute("data-page-number"),
  ).filter((value): value is string => value !== null);
}

vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?url", () => ({
  default: "/assets/pdf.worker.min.test.mjs",
}));

vi.mock("pdfjs-dist", () => {
  class TextLayer {
    container: HTMLElement;

    constructor({ container }: { container: HTMLElement }) {
      this.container = container;
    }

    async render() {
      const line = document.createElement("span");
      line.textContent = "Selectable page text";
      this.container.append(line);
    }

    cancel() {}
  }

  return {
    GlobalWorkerOptions: { workerSrc: "" },
    TextLayer,
    getDocument: pdf.getDocument,
  };
});


function page(number: number) {
  return {
    getViewport: ({ scale }: { scale: number }) => ({
      width: 600 * scale,
      height: 800 * scale,
      transform: [scale, 0, 0, -scale, 0, 800 * scale],
    }),
    render: (parameters: Record<string, unknown>) => {
      const cancel = vi.fn();
      pdf.renderCancels.push(cancel);
      pdf.renderParameters.push(parameters);
      return { promise: Promise.resolve(), cancel };
    },
    streamTextContent: () => new ReadableStream(),
    pageNumber: number,
  };
}


function sizedPage(number: number, unscaledWidth: number) {
  const value = page(number);
  return {
    ...value,
    getViewport: ({ scale }: { scale: number }) => ({
      width: unscaledWidth * scale,
      height: 800 * scale,
      transform: [scale, 0, 0, -scale, 0, 800 * scale],
    }),
  };
}


function readerProps() {
  return {
    documentId: "document-a",
    documentVersionId: "version-a",
    contentId: "content-a",
    filename: "paper.pdf",
  };
}


function setSelection(
  startContainer: Node,
  endContainer: Node,
  text: string,
  rects: Array<Pick<DOMRect, "left" | "top" | "right" | "bottom" | "width" | "height">>,
  collapsed = false,
  startOffset = 0,
  endOffset = endContainer instanceof Text
    ? endContainer.data.length
    : endContainer.childNodes.length,
) {
  let cleared = false;
  const removeAllRanges = vi.fn(() => { cleared = true; });
  const range = {
    collapsed,
    startContainer,
    endContainer,
    startOffset,
    endOffset,
    getClientRects: () => rects,
  } as unknown as Range;
  vi.spyOn(window, "getSelection").mockReturnValue({
    get isCollapsed() { return collapsed || cleared; },
    get rangeCount() { return cleared ? 0 : 1; },
    getRangeAt: () => range,
    removeAllRanges,
    toString: () => cleared ? "" : text,
  } as unknown as Selection);
  return removeAllRanges;
}


describe("PdfReader", () => {
  it("reloads the PDF after a content version changes without resetting the reader", async () => {
    const props = readerProps();
    const view = render(<PdfReader {...props} initialPage={2} initialScale={1.5} />);
    await screen.findByRole("toolbar", { name: "PDF 工具栏" });
    await waitFor(() => expect(pdf.getDocument).toHaveBeenCalledTimes(1));
    view.rerender(<PdfReader {...props} contentId="content-b" initialPage={2} initialScale={1.5} />);
    await waitFor(() => expect(pdf.getDocument).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("缩放比例")).toHaveTextContent("150%");
    expect(pdf.getDocument.mock.calls[1]?.[0].url).toBe("/api/documents/document-a/file");
  });
  it("restores the tab zoom and reports later zoom changes with the document identity", async () => {
    const onScaleChange = vi.fn();
    render(<PdfReader {...readerProps()} initialScale={1.5} onScaleChange={onScaleChange} />);
    await screen.findByRole("toolbar", { name: "PDF 工具栏" });
    expect(screen.getByLabelText("缩放比例")).toHaveTextContent("150%");
    await waitFor(() => expect(onScaleChange).toHaveBeenLastCalledWith("document-a", 1.5));
    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    expect(screen.getByLabelText("缩放比例")).toHaveTextContent("175%");
    expect(onScaleChange).toHaveBeenLastCalledWith("document-a", 1.75);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  beforeEach(() => {
    window.history.replaceState(null, "", "/?document=document-a");
    pdf.renderCancels.length = 0;
    pdf.renderParameters.length = 0;
    Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: 1 });
    pdf.getDocument.mockReset();
    pdf.getDocument.mockReturnValue({
      promise: Promise.resolve({
        numPages: 5,
        getPage: vi.fn(async (number: number) => page(number)),
      }),
      destroy: pdf.loadingDestroy,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/imports?active=true") {
          return { ok: true, json: async () => ({ items: [] }) };
        }
        if (url.endsWith("/annotations")) {
          return {
            ok: true,
            json: async () => ({
              items: [],
              document_id: "document-a",
              current_document_version_id: "version-a",
              content_id: "content-a",
            }),
          };
        }
        return {
          ok: true,
          json: async () => ({
            document_id: "document-a",
            current_document_version_id: "version-a",
            content_id: "content-a",
            filename: "paper.pdf",
            title: null,
            authors: null,
            relative_folder: "",
            size_bytes: 1024,
            status: "valid_pdf",
            duplicate_count: 1,
            parsing_state: "queued",
            cloud_policy: {
              auto_cloud_eligible: false,
              authorization_source: "registered_library",
            },
          }),
        };
      }),
    );
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      {} as CanvasRenderingContext2D,
    );
  });

  it("keeps a continuous page sequence while rendering only the current neighborhood", async () => {
    render(<App />);

    const toolbar = await screen.findByRole("toolbar", { name: "PDF 工具栏" });
    await waitFor(() => expect(pdf.getDocument).toHaveBeenCalledTimes(1));
    expect(pdf.getDocument).toHaveBeenCalledWith({
      url: "/api/documents/document-a/file",
      withCredentials: true,
      rangeChunkSize: 1024 * 1024,
      disableRange: false,
      cMapUrl: "/pdfjs/cmaps/",
      cMapPacked: true,
      standardFontDataUrl: "/pdfjs/standard_fonts/",
      wasmUrl: "/pdfjs/wasm/",
      iccUrl: "/pdfjs/iccs/",
    });
    expect(JSON.stringify(pdf.getDocument.mock.calls[0])).not.toMatch(/https?:\/\//);
    expect(within(toolbar).getByText("1 / 5")).toBeVisible();
    await waitFor(() => {
      expect(document.querySelectorAll("[data-page-number]")).toHaveLength(5);
    });
    expect(renderedPageNumbers()).toEqual(["1", "2", "3", "4", "5"]);
    expect(renderedPageNumbers('[data-rendered-page="true"]')).toEqual(["1", "2"]);

    fireEvent.click(within(toolbar).getByRole("button", { name: "下一页" }));

    await waitFor(() => {
      expect(renderedPageNumbers('[data-rendered-page="true"]')).toEqual(["1", "2", "3"]);
    });
  });

  it.each([
    { devicePixelRatio: 1, expectedScale: 1 },
    { devicePixelRatio: 2, expectedScale: 2 },
    { devicePixelRatio: 3, expectedScale: 2 },
  ])(
    "renders a sharp backing canvas at DPR $devicePixelRatio without changing CSS coordinates",
    async ({ devicePixelRatio, expectedScale }) => {
      Object.defineProperty(window, "devicePixelRatio", {
        configurable: true,
        value: devicePixelRatio,
      });
      render(<PdfReader {...readerProps()} />);
      const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
      const canvas = pageElement.querySelector("canvas");
      if (!(canvas instanceof HTMLCanvasElement)) throw new Error("mock canvas missing");

      await waitFor(() => {
        expect(canvas.width).toBe(600 * expectedScale);
        expect(canvas.height).toBe(800 * expectedScale);
      });
      expect(canvas.style.width).toBe("600px");
      expect(canvas.style.height).toBe("800px");
      expect(pdf.renderParameters[0]).toMatchObject({
        transform: [expectedScale, 0, 0, expectedScale, 0, 0],
      });
    },
  );

  it("supports typed physical page navigation and the complete v0.1 toolbar", async () => {
    render(<App />);
    const toolbar = await screen.findByRole("toolbar", { name: "PDF 工具栏" });
    const pageInput = within(toolbar).getByRole("spinbutton", { name: "PDF 页码" });

    fireEvent.change(pageInput, { target: { value: "5" } });
    fireEvent.keyDown(pageInput, { key: "Enter" });

    await waitFor(() => expect(within(toolbar).getByText("5 / 5")).toBeVisible());
    expect(within(toolbar).getByRole("button", { name: "上一页" })).toBeEnabled();
    expect(within(toolbar).getByRole("button", { name: "下一页" })).toBeDisabled();
    expect(within(toolbar).getByRole("button", { name: "放大" })).toBeEnabled();
    expect(within(toolbar).getByRole("button", { name: "缩小" })).toBeEnabled();
    expect(within(toolbar).getByRole("button", { name: "适合宽度" })).toBeEnabled();
    fireEvent.click(within(toolbar).getByRole("button", { name: "放大" }));
    expect(within(toolbar).getByRole("button", { name: "重置缩放" })).toBeEnabled();
    await waitFor(() => {
      expect(renderedPageNumbers('[data-rendered-page="true"]')).toEqual(["4", "5"]);
    });
  });

  it("updates the toolbar page as the reader is scrolled through a continuous document", async () => {
    render(<PdfReader {...readerProps()} />);
    const toolbar = await screen.findByRole("toolbar", { name: "PDF 工具栏" });
    const pages = document.querySelector(".pdf-reader__pages");
    if (!(pages instanceof HTMLDivElement)) throw new Error("reader pages missing");
    Object.defineProperty(pages, "clientHeight", { configurable: true, value: 800 });
    vi.spyOn(pages, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 900,
      bottom: 800,
      width: 900,
      height: 800,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    const regions = await screen.findAllByRole("region", { name: /PDF 第 \d+ 页/ });
    regions.forEach((page, index) => {
      vi.spyOn(page, "getBoundingClientRect").mockImplementation(() => {
        const top = index * 816 - pages.scrollTop;
        return {
          left: 0,
          top,
          right: 600,
          bottom: top + 800,
          width: 600,
          height: 800,
          x: 0,
          y: top,
          toJSON: () => ({}),
        };
      });
    });
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(performance.now());
      return 1;
    });

    pages.scrollTop = 1632;
    fireEvent.scroll(pages);

    await waitFor(() => expect(within(toolbar).getByText("3 / 5")).toBeVisible());
    expect(renderedPageNumbers('[data-rendered-page="true"]')).toEqual(["2", "3", "4"]);
  });

  it("fits width using the current physical page in a mixed-width document", async () => {
    pdf.getDocument.mockReturnValue({
      promise: Promise.resolve({
        numPages: 2,
        getPage: vi.fn(async (number: number) => sizedPage(number, number === 1 ? 450 : 900)),
      }),
      destroy: pdf.loadingDestroy,
    });
    render(<PdfReader {...readerProps()} />);
    const toolbar = await screen.findByRole("toolbar", { name: "PDF 工具栏" });
    const pages = document.querySelector(".pdf-reader__pages");
    if (!(pages instanceof HTMLElement)) throw new Error("reader pages missing");
    Object.defineProperty(pages, "clientWidth", { configurable: true, value: 932 });
    fireEvent.click(within(toolbar).getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(within(toolbar).getByText("2 / 2")).toBeVisible());

    fireEvent.click(within(toolbar).getByRole("button", { name: "适合宽度" }));

    await waitFor(() => {
      expect(within(toolbar).getByRole("status", { name: "缩放比例" })).toHaveTextContent("100%");
    });
  });

  it("captures normalized multi-line text and bounded same-page context", async () => {
    const selected: SelectedText[] = [];
    render(<PdfReader {...readerProps()} onSelectionChange={(value) => selected.push(value)} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textLayer = pageElement.querySelector(".textLayer") as HTMLElement;
    textLayer.replaceChildren();
    const before = document.createTextNode("before context ");
    const first = document.createTextNode("first line");
    const second = document.createTextNode(" second line");
    const after = document.createTextNode(" after context");
    textLayer.append(before, first, second, after);
    vi.spyOn(pageElement, "getBoundingClientRect").mockReturnValue({
      left: 10,
      top: 20,
      right: 610,
      bottom: 820,
      width: 600,
      height: 800,
      x: 10,
      y: 20,
      toJSON: () => ({}),
    });
    const removeAllRanges = setSelection(first, second, "first line\n second line", [
      { left: 30, top: 40, right: 130, bottom: 60, width: 100, height: 20 },
      { left: 30, top: 65, right: 150, bottom: 85, width: 120, height: 20 },
    ]);

    fireEvent.pointerUp(pageElement);

    expect(selected).toHaveLength(1);
    expect(selected[0]).toMatchObject({
      documentId: "document-a",
      documentVersionId: "version-a",
      contentId: "content-a",
      pdfPage: 1,
      text: "first line second line",
      contextBefore: "before context",
      contextAfter: "after context",
      rects: [
        { x: 20, y: 20, width: 100, height: 20 },
        { x: 20, y: 45, width: 120, height: 20 },
      ],
      viewportAnchor: { x: 150, y: 85 },
    });
    expect(removeAllRanges).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBeVisible();
  });

  it("uses visual pointer hits instead of interleaved DOM order for a drag", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textLayer = pageElement.querySelector(".textLayer") as HTMLElement;
    textLayer.replaceChildren();
    const sidebarSpan = document.createElement("span");
    const firstSpan = document.createElement("span");
    const footerSpan = document.createElement("span");
    const secondSpan = document.createElement("span");
    sidebarSpan.textContent = "Published: October 25, 2021";
    firstSpan.textContent = "Microsatellites are neutral,";
    footerSpan.textContent = "PLOS ONE October 25, 2021";
    secondSpan.textContent = "codominant markers.";
    textLayer.append(sidebarSpan, firstSpan, footerSpan, secondSpan);
    const nodes = [
      sidebarSpan.firstChild as Text,
      firstSpan.firstChild as Text,
      footerSpan.firstChild as Text,
      secondSpan.firstChild as Text,
    ];
    const bounds = new Map<Node, DOMRect>([
      [nodes[0] as Node, new DOMRect(20, 100, 250, 20)],
      [nodes[1] as Node, new DOMRect(330, 100, 290, 20)],
      [nodes[2] as Node, new DOMRect(20, 500, 280, 20)],
      [nodes[3] as Node, new DOMRect(330, 124, 200, 20)],
    ]);
    vi.spyOn(pageElement, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 700, 600),
    );
    const nativeSelection = { removeAllRanges: vi.fn() } as unknown as Selection;
    vi.spyOn(window, "getSelection").mockReturnValue(nativeSelection);
    const rangeState: { node: Text | null; start: number; end: number } = {
      node: null,
      start: 0,
      end: 0,
    };
    vi.spyOn(document, "createRange").mockImplementation(() => ({
      setStart(node: Text, offset: number) { rangeState.node = node; rangeState.start = offset; },
      setEnd(node: Text, offset: number) { rangeState.node = node; rangeState.end = offset; },
      getClientRects() {
        const base = rangeState.node === null ? undefined : bounds.get(rangeState.node);
        if (base === undefined || rangeState.node === null) return [];
        const width = base.width / Math.max(1, rangeState.node.data.length);
        return [new DOMRect(
          base.left + rangeState.start * width,
          base.top,
          (rangeState.end - rangeState.start) * width,
          base.height,
        )];
      },
    }) as unknown as Range);
    Object.defineProperty(document, "caretPositionFromPoint", {
      configurable: true,
      value: vi.fn((x: number, y: number) => y < 122
        ? { offsetNode: nodes[0], offset: nodes[0]?.data.length ?? 0 }
        : { offsetNode: nodes[3], offset: nodes[3]?.data.length ?? 0 }),
    });

    fireEvent.pointerDown(firstSpan, { clientX: 330, clientY: 110, pointerId: 1 });
    fireEvent.pointerMove(pageElement, { clientX: 530, clientY: 134, pointerId: 1 });
    expect(screen.queryByRole("button", { name: "翻译所选文字" })).not.toBeInTheDocument();
    fireEvent.pointerUp(pageElement, { clientX: 530, clientY: 134, pointerId: 1 });

    expect(onSelection).toHaveBeenCalledTimes(1);
    expect(onSelection.mock.calls[0]?.[0]).toMatchObject({
      text: "Microsatellites are neutral, codominant markers.",
    });
    expect(onSelection.mock.calls[0]?.[0].text).not.toContain("Published");
    expect(onSelection.mock.calls[0]?.[0].text).not.toContain("PLOS ONE");
    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBeVisible();
    expect(nativeSelection.removeAllRanges).toHaveBeenCalled();
  });

  it("does not clear the frozen selection before the translate button click", async () => {
    const onTranslateSelection = vi.fn();
    render(<PdfReader {...readerProps()} onTranslateSelection={onTranslateSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textNode = await renderedTextNode(pageElement);
    setSelection(textNode, textNode, "selected", [
      { left: 20, top: 20, right: 100, bottom: 40, width: 80, height: 20 },
    ]);
    fireEvent.pointerUp(pageElement);
    const button = screen.getByRole("button", { name: "翻译所选文字" });

    fireEvent.pointerUp(button);
    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBe(button);
    fireEvent.click(button);

    expect(onTranslateSelection).toHaveBeenCalledTimes(1);
    expect(onTranslateSelection.mock.calls[0]?.[0]).toMatchObject({ text: "selected" });
  });

  it("copies each reader's frozen selection and leaves native or editable text alone", async () => {
    render(<><PdfReader {...readerProps()} onAnnotateSelection={vi.fn()} />
      <PdfReader {...readerProps()} documentId="document-b" filename="other.pdf" /></>);
    for (const [name, text] of [["paper.pdf", "left text"], ["other.pdf", "right text"]]) {
      const reader = await screen.findByRole("region", { name: `阅读 ${name}` });
      const page = within(reader).getByRole("region", { name: "PDF 第 1 页" });
      const node = await renderedTextNode(page);
      setSelection(node, node, text!, [{ left: 20, top: 20, right: 100, bottom: 40, width: 80, height: 20 }]);
      fireEvent.pointerUp(page);
      expect(document.activeElement).toBe(reader.querySelector(".pdf-reader__pages"));
      const setData = vi.fn();
      expect(fireEvent.copy(document.activeElement!, { clipboardData: { setData } })).toBe(false);
      expect(setData).toHaveBeenCalledWith("text/plain", text);
    }
    const left = screen.getByRole("region", { name: "阅读 paper.pdf" });
    const setData = vi.fn();
    fireEvent.copy(left.querySelector(".pdf-reader__pages")!, { clipboardData: { setData } });
    expect(setData).toHaveBeenCalledWith("text/plain", "left text");
    setData.mockClear();
    fireEvent.click(within(left).getByRole("button", { name: "评论" }));
    fireEvent.copy(within(left).getByRole("textbox", { name: "评论内容" }), { clipboardData: { setData } });
    expect(setData).not.toHaveBeenCalled();
    const node = await renderedTextNode(within(left).getByRole("region", { name: "PDF 第 1 页" }));
    setSelection(node, node, "native selection", []);
    fireEvent.copy(left.querySelector(".pdf-reader__pages")!, { clipboardData: { setData } });
    expect(setData).not.toHaveBeenCalled();
    window.getSelection()?.removeAllRanges();
    fireEvent.click(within(left).getByRole("button", { name: "取消" }));
    fireEvent.copy(left.querySelector(".pdf-reader__pages")!, { clipboardData: { setData } });
    expect(setData).not.toHaveBeenCalled();
  });

  it("preserves a frozen selection when the user opens the right-click menu", async () => {
    render(<PdfReader {...readerProps()} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textNode = await renderedTextNode(pageElement);
    setSelection(textNode, textNode, "selected", [
      { left: 20, top: 20, right: 100, bottom: 40, width: 80, height: 20 },
    ]);
    fireEvent.pointerUp(pageElement, { button: 0 });
    const translate = screen.getByRole("button", { name: "翻译所选文字" });

    fireEvent.pointerDown(pageElement, { button: 2 });
    fireEvent.pointerUp(pageElement, { button: 2 });

    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBe(translate);
  });

  it("ignores blank selections and rejects cross-page selections locally", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pages = await screen.findAllByRole("region", { name: /PDF 第 \d+ 页/ });
    const firstText = await renderedTextNode(pages[0] as HTMLElement);
    const secondText = await renderedTextNode(pages[1] as HTMLElement);
    setSelection(firstText, firstText, "   \n ", [], true);
    fireEvent.pointerUp(pages[0] as HTMLElement);
    expect(onSelection).not.toHaveBeenCalled();

    const removeAllRanges = setSelection(firstText, secondText, "cross page", [
      { left: 1, top: 1, right: 2, bottom: 2, width: 1, height: 1 },
    ]);
    fireEvent.pointerUp(pages[0] as HTMLElement);

    expect(onSelection).not.toHaveBeenCalled();
    expect(screen.getByText("请在同一栏选择一句或一段")).toBeVisible();
    expect(removeAllRanges).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "翻译所选文字" })).not.toBeInTheDocument();
  });

  it("keeps long text selected but disables translation above 8000 Unicode code points", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textNode = await renderedTextNode(pageElement);
    const oversized = "😀".repeat(8001);
    const removeAllRanges = setSelection(textNode, textNode, oversized, [
      { left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 },
    ]);

    fireEvent.pointerUp(pageElement);

    expect(onSelection).toHaveBeenCalledTimes(1);
    expect(onSelection.mock.calls[0]?.[0]).toMatchObject({
      text: oversized,
      translationEligible: false,
    });
    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBeDisabled();
    expect(screen.getByText("超过 8,000 字")).toBeVisible();
    expect(removeAllRanges).toHaveBeenCalledTimes(1);
  });

  it("keeps an active native selection when edge dragging scrolls the reader", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textNode = await renderedTextNode(pageElement);
    const textElement = textNode.parentElement;
    const pages = document.querySelector(".pdf-reader__pages");
    if (textElement === null || !(pages instanceof HTMLElement)) {
      throw new Error("reader selection surface missing");
    }

    fireEvent.pointerDown(textElement, { button: 0 });
    const removeAllRanges = setSelection(textNode, textNode, "edge scrolled selection", [
      { left: 20, top: 40, right: 170, bottom: 60, width: 150, height: 20 },
    ]);
    fireEvent.scroll(pages);

    expect(removeAllRanges).not.toHaveBeenCalled();
    fireEvent.pointerUp(textElement);
    expect(onSelection).toHaveBeenCalledTimes(1);
    expect(onSelection.mock.calls[0]?.[0]).toMatchObject({
      text: "edge scrolled selection",
    });
  });

  it("rejects cross-column selection but keeps a long same-column selection", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textNode = await renderedTextNode(pageElement);

    const crossColumnClear = setSelection(textNode, textNode, "left and right", [
      { left: 20, top: 40, right: 170, bottom: 60, width: 150, height: 20 },
      { left: 300, top: 40, right: 450, bottom: 60, width: 150, height: 20 },
    ]);
    fireEvent.pointerUp(pageElement);

    expect(onSelection).not.toHaveBeenCalled();
    expect(screen.getByText("请在同一栏选择一句或一段")).toBeVisible();
    expect(crossColumnClear).toHaveBeenCalledTimes(1);

    const longSelectionClear = setSelection(
      textNode,
      textNode,
      "many lines",
      Array.from({ length: 25 }, (_, index) => ({
        left: 20,
        top: 40 + index * 22,
        right: 170,
        bottom: 60 + index * 22,
        width: 150,
        height: 20,
      })),
    );
    fireEvent.pointerUp(pageElement);

    expect(onSelection).toHaveBeenCalledTimes(1);
    expect(onSelection.mock.calls[0]?.[0]).toMatchObject({
      text: "many lines",
      translationEligible: true,
    });
    expect(longSelectionClear).toHaveBeenCalledTimes(1);
  });

  it("allows an intentional cross-column selection while Alt is held", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textNode = await renderedTextNode(pageElement);
    setSelection(textNode, textNode, "left and right", [
      { left: 20, top: 40, right: 170, bottom: 60, width: 150, height: 20 },
      { left: 300, top: 40, right: 450, bottom: 60, width: 150, height: 20 },
    ]);

    fireEvent.pointerUp(pageElement, { altKey: true });

    expect(onSelection).toHaveBeenCalledTimes(1);
    expect(onSelection.mock.calls[0]?.[0]).toMatchObject({ text: "left and right" });
  });

  it("restores the last valid same-column range when dragging briefly crosses columns", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textNode = await renderedTextNode(pageElement);
    const validRange = {
      collapsed: false,
      startContainer: textNode,
      endContainer: textNode,
      startOffset: 0,
      endOffset: 1,
      getClientRects: () => [
        { left: 20, top: 40, right: 170, bottom: 60, width: 150, height: 20 },
      ],
      cloneRange: () => validRange,
    } as unknown as Range;
    const crossColumnRange = {
      ...validRange,
      getClientRects: () => [
        { left: 20, top: 40, right: 170, bottom: 60, width: 150, height: 20 },
        { left: 300, top: 40, right: 450, bottom: 60, width: 150, height: 20 },
      ],
      cloneRange: () => crossColumnRange,
    } as unknown as Range;
    let activeRange = validRange;
    let rangeCount = 1;
    const addRange = vi.fn((range: Range) => {
      activeRange = range;
      rangeCount = 1;
    });
    const removeAllRanges = vi.fn(() => { rangeCount = 0; });
    vi.spyOn(window, "getSelection").mockReturnValue({
      get isCollapsed() { return rangeCount === 0; },
      get rangeCount() { return rangeCount; },
      getRangeAt: () => activeRange,
      addRange,
      removeAllRanges,
      toString: () => activeRange === validRange ? "selected" : "selected across columns",
    } as unknown as Selection);

    fireEvent.pointerDown(pageElement);
    fireEvent.pointerMove(pageElement);
    activeRange = crossColumnRange;
    fireEvent.pointerMove(pageElement);
    fireEvent.pointerUp(pageElement);

    expect(addRange).toHaveBeenCalledWith(validRange);
    expect(onSelection).toHaveBeenCalledTimes(1);
    expect(onSelection.mock.calls[0]?.[0]).toMatchObject({ text: "selected" });
  });

  it("keeps the nearest 500 Unicode code points before a selection", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textLayer = pageElement.querySelector(".textLayer") as HTMLElement;
    textLayer.replaceChildren();
    const before = document.createTextNode(`${"x".repeat(600)}near `);
    const selected = document.createTextNode("selected");
    textLayer.append(before, selected);
    setSelection(selected, selected, "selected", [
      { left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 },
    ]);

    fireEvent.pointerUp(pageElement);

    const value = onSelection.mock.calls[0]?.[0] as SelectedText;
    expect(Array.from(value.contextBefore)).toHaveLength(500);
    expect(value.contextBefore.endsWith("near")).toBe(true);
  });

  it("derives context from the selected Range when the phrase repeats", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textLayer = pageElement.querySelector(".textLayer") as HTMLElement;
    textLayer.replaceChildren();
    const before = document.createTextNode("alpha repeat middle ");
    const selected = document.createTextNode("repeat");
    const after = document.createTextNode(" tail repeat");
    textLayer.append(before, selected, after);
    setSelection(selected, selected, "repeat", [
      { left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 },
    ]);

    fireEvent.pointerUp(pageElement);

    const value = onSelection.mock.calls[0]?.[0] as SelectedText;
    expect(value.contextBefore).toBe("alpha repeat middle");
    expect(value.contextAfter).toBe("tail repeat");
  });

  it("shows a local hint for a non-collapsed canvas-only selection", async () => {
    const onSelection = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionChange={onSelection} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const canvas = pageElement.querySelector("canvas");
    if (canvas === null) throw new Error("mock canvas missing");
    setSelection(canvas, canvas, "canvas selection", [
      { left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 },
    ]);

    fireEvent.pointerUp(pageElement);

    expect(onSelection).not.toHaveBeenCalled();
    expect(screen.getByText("请在同一栏选择一句或一段")).toBeVisible();
  });

  it("recalculates overlay coordinates on zoom and keeps a frozen selection while pages change", async () => {
    const onSelectionClear = vi.fn();
    render(<PdfReader {...readerProps()} onSelectionClear={onSelectionClear} />);
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    onSelectionClear.mockClear();
    const textNode = await renderedTextNode(pageElement);
    vi.spyOn(pageElement, "getBoundingClientRect").mockReturnValue({
      left: 10,
      top: 10,
      right: 610,
      bottom: 810,
      width: 600,
      height: 800,
      x: 10,
      y: 10,
      toJSON: () => ({}),
    });
    setSelection(textNode, textNode, "selected", [
      { left: 30, top: 30, right: 50, bottom: 50, width: 20, height: 20 },
    ]);
    fireEvent.pointerUp(pageElement);
    const highlight = await screen.findByTestId("selection-rect");
    expect(highlight).toHaveStyle({ left: "20px", width: "20px" });

    fireEvent.click(screen.getByRole("button", { name: "放大" }));

    expect(highlight).toHaveStyle({ left: "25px", width: "25px" });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBeVisible();
    expect(onSelectionClear).not.toHaveBeenCalled();
  });

  it("keeps the frozen selection on a same-document annotation version", async () => {
    const onTranslateSelection = vi.fn();
    const { rerender } = render(
      <PdfReader {...readerProps()} onTranslateSelection={onTranslateSelection} />,
    );
    const pageElement = await screen.findByRole("region", { name: "PDF 第 1 页" });
    const textNode = await renderedTextNode(pageElement);
    setSelection(textNode, textNode, "selected", [
      { left: 1, top: 1, right: 11, bottom: 11, width: 10, height: 10 },
    ]);
    fireEvent.pointerUp(pageElement);
    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBeVisible();

    rerender(
      <PdfReader
        {...readerProps()}
        contentId="content-b"
        documentVersionId="version-b"
        onTranslateSelection={onTranslateSelection}
      />,
    );

    const translate = await screen.findByRole("button", { name: "翻译所选文字" });
    expect(translate).toBeVisible();
    fireEvent.click(translate);
    expect(onTranslateSelection).toHaveBeenCalledWith(expect.objectContaining({
      documentVersionId: "version-b",
      contentId: "content-b",
      text: "selected",
    }));
  });

  it("reloads an annotation version without closing the reader", async () => {
    const { rerender } = render(<PdfReader {...readerProps()} />);
    const toolbar = await screen.findByRole("toolbar", { name: "PDF 工具栏" });
    expect(toolbar).toBeVisible();

    rerender(
      <PdfReader
        {...readerProps()}
        contentId="content-b"
        documentVersionId="version-b"
      />,
    );

    expect(screen.getByRole("toolbar", { name: "PDF 工具栏" })).toBeVisible();
    expect(screen.queryByText(/正在打开/)).not.toBeInTheDocument();
    await waitFor(() => expect(pdf.getDocument).toHaveBeenCalledTimes(2));
  });

  it("loads a new PDF when the document itself changes", async () => {
    const { rerender } = render(<PdfReader {...readerProps()} />);
    await screen.findByRole("toolbar", { name: "PDF 工具栏" });

    rerender(
      <PdfReader
        {...readerProps()}
        contentId="content-b"
        documentId="document-b"
        documentVersionId="version-b"
      />,
    );

    await waitFor(() => expect(pdf.getDocument).toHaveBeenCalledTimes(2));
    expect(pdf.getDocument).toHaveBeenLastCalledWith(expect.objectContaining({
      url: "/api/documents/document-b/file",
    }));
  });

  it("keeps page, zoom, and reader scroll after an annotation identity update", async () => {
    const { rerender } = render(<PdfReader {...readerProps()} />);
    const toolbar = await screen.findByRole("toolbar", { name: "PDF 工具栏" });
    fireEvent.click(within(toolbar).getByRole("button", { name: "下一页" }));
    fireEvent.click(within(toolbar).getByRole("button", { name: "放大" }));
    await waitFor(() => expect(within(toolbar).getByText("2 / 5")).toBeVisible());
    expect(within(toolbar).getByRole("status", { name: "缩放比例" })).toHaveTextContent("125%");
    await act(async () => {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    });
    const pages = document.querySelector(".pdf-reader__pages");
    if (!(pages instanceof HTMLElement)) throw new Error("reader pages missing");
    pages.scrollTop = 420;
    pages.scrollLeft = 35;
    fireEvent.scroll(pages);

    rerender(
      <PdfReader
        {...readerProps()}
        contentId="content-b"
        documentVersionId="version-b"
      />,
    );

    const reloadedToolbar = await screen.findByRole("toolbar", { name: "PDF 工具栏" });
    await waitFor(() => expect(within(reloadedToolbar).getByText("2 / 5")).toBeVisible());
    expect(within(reloadedToolbar).getByRole("status", { name: "缩放比例" })).toHaveTextContent("125%");
    await waitFor(() => {
      const reloadedPages = document.querySelector(".pdf-reader__pages");
      expect(reloadedPages).toBeInstanceOf(HTMLElement);
      expect((reloadedPages as HTMLElement).scrollTop).toBe(420);
      expect((reloadedPages as HTMLElement).scrollLeft).toBe(35);
    });
    expect(pdf.getDocument).toHaveBeenCalledTimes(2);
  });

  it("calculates page index mathematically from scroll position and page heights", async () => {
    const { findCurrentPageIndex } = await import("./PdfReader");
    const pageSizes = [
      { width: 600, height: 800 },
      { width: 600, height: 800 },
      { width: 600, height: 1000 },
      { width: 600, height: 800 },
    ];

    // Page 0 (paddingTop 24 + height 800 + gap 24 = 848): targetOffset at 0 is 400 < 848
    expect(findCurrentPageIndex(0, 800, pageSizes, 1)).toBe(0);
    expect(findCurrentPageIndex(200, 800, pageSizes, 1)).toBe(0);

    // Scrolled into page 1: center lands in page 1
    expect(findCurrentPageIndex(848, 800, pageSizes, 1)).toBe(1);

    // Scrolled into page 2 (height 1000)
    expect(findCurrentPageIndex(1672, 800, pageSizes, 1)).toBe(2);

    // Zoom scale 1.5
    expect(findCurrentPageIndex(0, 800, pageSizes, 1.5)).toBe(0);
    // Page 0 at 1.5 scale is 24 + 1200 + 24 = 1248
    expect(findCurrentPageIndex(1248, 800, pageSizes, 1.5)).toBe(1);

    // Explicit custom gap and padding
    expect(findCurrentPageIndex(0, 800, pageSizes, 1, 16, 16)).toBe(0);
    expect(findCurrentPageIndex(816, 800, pageSizes, 1, 16, 16)).toBe(1);

    // Empty page sizes fallback
    expect(findCurrentPageIndex(500, 800, [], 1)).toBe(0);
  });
});
