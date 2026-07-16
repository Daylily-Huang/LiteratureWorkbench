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
  afterEach(cleanup);

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
    render(<SelectionOverlay scale={1} selected={{ ...selected, translationEligible: false }} />);
    expect(screen.getByRole("button", { name: "翻译所选文字" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "高亮" })).toBeEnabled();
    expect(screen.getByText("超过 8,000 字")).toBeVisible();
  });
});
