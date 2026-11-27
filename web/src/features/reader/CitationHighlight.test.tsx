import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { CitationHighlight } from "./CitationHighlight";


describe("CitationHighlight", () => {
  afterEach(cleanup);

  it("maps bbox_1000 rects onto the rendered page box and sets aria-hidden", () => {
    const { container, getAllByTestId } = render(
      <CitationHighlight
        nonce={1}
        rects={[[100, 200, 400, 260], [100, 500, 400, 560]]}
        renderedHeight={800}
        renderedWidth={500}
      />,
    );

    const root = container.querySelector(".citation-highlight");
    expect(root).toHaveAttribute("aria-hidden", "true");

    const rects = getAllByTestId("citation-highlight-rect");
    expect(rects).toHaveLength(2);
    expect(rects[0]).toHaveStyle({
      left: "50px",
      top: "160px",
      width: "150px",
      height: "48px",
    });
    expect(rects[1]).toHaveStyle({ top: "400px" });
  });

  it("renders nothing without rects", () => {
    const { container } = render(
      <CitationHighlight nonce={1} rects={[]} renderedHeight={800} renderedWidth={500} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("re-keys on nonce so the flash replays for repeat jumps", () => {
    const { container, rerender } = render(
      <CitationHighlight
        nonce={1}
        rects={[[100, 200, 400, 260]]}
        renderedHeight={800}
        renderedWidth={500}
      />,
    );
    const first = container.querySelector(".citation-highlight");

    rerender(
      <CitationHighlight
        nonce={2}
        rects={[[100, 200, 400, 260]]}
        renderedHeight={800}
        renderedWidth={500}
      />,
    );
    const second = container.querySelector(".citation-highlight");

    expect(first).not.toBe(second);
  });

  it("validates pdf rects strictly within 0-1000 coordinate bounds", async () => {
    const { isValidPdfRect } = await import("./CitationHighlight");
    expect(isValidPdfRect([0, 0, 1000, 1000])).toBe(true);
    expect(isValidPdfRect([10, 20, 300, 80])).toBe(true);

    expect(isValidPdfRect([-1, 20, 300, 80])).toBe(false);
    expect(isValidPdfRect([10, 20, 1001, 80])).toBe(false);
    expect(isValidPdfRect([10, 20, 300, 1001])).toBe(false);
    expect(isValidPdfRect([300, 20, 100, 80])).toBe(false);
    expect(isValidPdfRect([10, 80, 300, 20])).toBe(false);
    expect(isValidPdfRect([10, 20, Number.NaN, 80])).toBe(false);
    expect(isValidPdfRect([10, 20, Number.POSITIVE_INFINITY, 80])).toBe(false);
    expect(isValidPdfRect("not a rect")).toBe(false);
    expect(isValidPdfRect([1, 2, 3])).toBe(false);
  });
});
