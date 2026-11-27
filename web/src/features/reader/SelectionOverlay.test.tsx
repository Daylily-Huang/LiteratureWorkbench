import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SelectionOverlay } from "./SelectionOverlay";
import type { SelectedText } from "./selection";


const selected: SelectedText = {
  documentId: "document-a",
  documentVersionId: "version-a",
  contentId: "a".repeat(64),
  pdfPage: 1,
  text: "selected text",
  contextBefore: "",
  contextAfter: "",
  rects: [{ x: 10, y: 20, width: 100, height: 18 }],
  pdfQuads: [[10, 780, 110, 780, 10, 762, 110, 762]],
  buttonAnchor: { x: 110, y: 38 },
  viewportAnchor: { x: 110, y: 38 },
  translationEligible: true,
};


describe("SelectionOverlay", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("copies the selected original text and reports a rejected clipboard write", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<SelectionOverlay scale={1} selected={selected} />);
    fireEvent.click(screen.getByRole("button", { name: "复制所选文字" }));
    await screen.findByText("已复制");
    expect(writeText).toHaveBeenCalledWith("selected text");
    writeText.mockRejectedValue(new Error("permission denied"));
    fireEvent.click(screen.getByRole("button", { name: "复制所选文字" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Ctrl+C");
  });

  it("offers translation, colored markup, comments, and cancel beside the selection", () => {
    const annotate = vi.fn();
    const cancel = vi.fn();
    render(
      <SelectionOverlay
        onAnnotate={annotate}
        onCancel={cancel}
        scale={1}
        selected={selected}
      />,
    );

    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "高亮" }));
    fireEvent.click(screen.getByRole("button", { name: "green 高亮" }));
    expect(annotate).toHaveBeenCalledWith("highlight", "green");
    expect(window.localStorage.getItem("lw-highlight-color")).toBe("green");

    fireEvent.click(screen.getByRole("button", { name: "评论" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论内容" }), {
      target: { value: "important" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存评论" }));
    expect(annotate).toHaveBeenCalledWith("comment", "blue", "important");

    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("keeps annotation tools enabled when translation exceeds the 8000-character limit", () => {
    render(<SelectionOverlay scale={1} selected={{ ...selected, translationEligible: false }} onAnnotate={vi.fn()} />);
    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "高亮" })).toBeEnabled();
    expect(screen.getByText("超过 8,000 字")).toBeVisible();
  });

  it("offers cleaned copy and Shift-click source text without changing the selection", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<SelectionOverlay scale={1} selected={{ ...selected, text: "individu- als", rawText: "individu-\nals", copyText: "individuals" }} />);
    fireEvent.click(screen.getByRole("button", { name: "复制所选文字" }));
    await screen.findByText("已复制");
    expect(writeText).toHaveBeenLastCalledWith("individuals");
    fireEvent.click(screen.getByRole("button", { name: "复制所选文字" }), { shiftKey: true });
    expect(writeText).toHaveBeenLastCalledWith("individu-\nals");
  });
});


it("clamps the toolbar inside the inner scroll client width so right-edge selections are never clipped", () => {
  const { container } = render(
    <div className="pdf-reader__pages" data-testid="clip">
      <div data-testid="page" style={{ position: "relative", width: "900px", height: "1200px" }}>
        <SelectionOverlay
          scale={1}
          selected={{ ...selected, buttonAnchor: { x: 860, y: 120 } }}
          onAnnotate={vi.fn()}
        />
      </div>
    </div>,
  );
  const clip = screen.getByTestId("clip");
  const overlay = container.querySelector(".selection-overlay") as HTMLElement;
  const tools = container.querySelector(".selection-overlay__tools") as HTMLElement;
  vi.spyOn(clip, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, right: 620, bottom: 800, width: 620, height: 800, x: 0, y: 0, toJSON: () => ({}) });
  Object.defineProperty(clip, "clientWidth", { configurable: true, value: 600 });
  Object.defineProperty(clip, "clientHeight", { configurable: true, value: 780 });
  vi.spyOn(overlay, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, right: 900, bottom: 1200, width: 900, height: 1200, x: 0, y: 0, toJSON: () => ({}) });
  vi.spyOn(tools, "getBoundingClientRect").mockReturnValue({ left: 860, top: 126, right: 1180, bottom: 162, width: 320, height: 36, x: 860, y: 126, toJSON: () => ({}) });
  fireEvent(window, new Event("resize"));
  const computedLeft = Number.parseFloat(tools.style.left);
  expect(computedLeft).toBeLessThanOrEqual(600 - 320 - 10);
  expect(computedLeft).toBeGreaterThanOrEqual(10);
});
