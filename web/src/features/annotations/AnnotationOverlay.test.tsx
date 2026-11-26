import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AnnotationOverlay } from "./AnnotationOverlay";
import type { PdfAnnotation } from "./annotations";

const transform = [1, 0, 0, 1, 0, 0];
const annotation: PdfAnnotation = {
  annotation_id: "left-note", page: 1, type: "comment", owned: true, read_only: false,
  selected_text: "Original source", comment: "需要核对研究方法。\n这里保留第二行。",
  subject: "", color: [1, 1, 0], quads: [[10, 10, 100, 10, 10, 30, 100, 30]],
};
const items = [annotation];
function tick(ms: number) { act(() => vi.advanceTimersByTime(ms)); }
function hover() {
  fireEvent.pointerEnter(screen.getByRole("button", { name: "打开批注：Original source" }));
  tick(350);
}

describe("annotation hover preview", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  it("previews without opening the sidebar, and keeps long comments open while reading", () => {
    const onOpen = vi.fn();
    render(<AnnotationOverlay annotations={items} pageTransform={transform} onOpen={onOpen} />);
    const marker = screen.getByRole("button", { name: "打开批注：Original source" });
    fireEvent.pointerEnter(marker); tick(200);
    expect(screen.queryByRole("dialog")).toBeNull();
    tick(150);
    const card = screen.getByRole("dialog", { name: "批注预览" });
    expect(card).toHaveTextContent("需要核对研究方法。");
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.pointerLeave(marker); tick(100); fireEvent.pointerEnter(card); tick(400);
    fireEvent.scroll(card.querySelector(".annotation-preview__body")!);
    expect(screen.getByRole("dialog")).toBe(card);
    fireEvent.click(screen.getByRole("button", { name: "在笔记中查看" }));
    expect(onOpen).toHaveBeenCalledWith("left-note");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("dismisses on Escape, page scroll and selection start", () => {
    render(<AnnotationOverlay annotations={items} pageTransform={transform} />);
    hover(); fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    hover(); fireEvent.scroll(window);
    expect(screen.queryByRole("dialog")).toBeNull();
    hover(); fireEvent.pointerDown(document.body); tick(500);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancels delayed previews when the document annotations change", () => {
    const { rerender } = render(<AnnotationOverlay annotations={items} pageTransform={transform} />);
    fireEvent.pointerEnter(screen.getByRole("button", { name: "打开批注：Original source" }));
    const other = [{ ...annotation, annotation_id: "right-note", comment: "另一篇文献的批注" }];
    rerender(<AnnotationOverlay annotations={other} pageTransform={transform} />);
    tick(500);
    expect(screen.queryByRole("dialog")).toBeNull();
    hover();
    expect(screen.getByRole("dialog")).toHaveTextContent("另一篇文献的批注");
    expect(screen.getByRole("dialog")).not.toHaveTextContent("需要核对研究方法");
  });

  it("distinguishes a highlight without a comment from a written note", () => {
    render(<AnnotationOverlay annotations={[{ ...annotation, type: "highlight", comment: "", read_only: true }]} pageTransform={transform} />);
    fireEvent.pointerEnter(screen.getByRole("button", { name: "只读批注：Original source" })); tick(350);
    expect(screen.getByRole("dialog")).toHaveTextContent("这处已标注，尚未填写评论。");
    expect(screen.getByRole("dialog")).toHaveTextContent("只读");
  });

  it("keeps comments accessible when another mark overlaps, including multiple notes", () => {
    const note2 = { ...annotation, annotation_id: "second-note", comment: "另一条评论" };
    const underline = { ...annotation, annotation_id: "underline", type: "underline", comment: "" };
    const open = vi.fn();
    render(<AnnotationOverlay annotations={[annotation, underline, note2]} pageTransform={transform} onOpen={open} />);
    const markers = screen.getAllByRole("button", { name: "打开批注：Original source" });
    expect(markers[0]).toHaveStyle({ zIndex: 2 });
    expect(markers[1]).toHaveStyle({ zIndex: 1 });
    fireEvent.pointerEnter(markers[2]!); tick(350);
    expect(screen.getByRole("dialog")).toHaveTextContent("另一条评论");
    expect(screen.getByRole("dialog")).toHaveTextContent("需要核对研究方法");
    fireEvent.click(screen.getByRole("button", { name: "在笔记中查看这条评论" }));
    expect(open).toHaveBeenCalledWith("left-note");
  });

  it("reopens a preview after Escape while the pointer remains in the same annotation", () => {
    const { container } = render(<section><AnnotationOverlay annotations={items} pageTransform={transform} /></section>);
    const page = container.querySelector("section")!;
    const marker = screen.getByRole("button", { name: "打开批注：Original source" });
    vi.spyOn(marker, "getBoundingClientRect").mockReturnValue(new DOMRect(10, 10, 90, 20));
    const move = (x: number) => {
      const event = new MouseEvent("pointermove", { bubbles: true, clientX: x, clientY: 20, buttons: 0 });
      fireEvent(page, event);
    };
    move(20); tick(350);
    expect(screen.getByRole("dialog", { name: "批注预览" })).toBeVisible();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    move(23); tick(350);
    expect(screen.getByRole("dialog", { name: "批注预览" })).toBeVisible();
  });
});
