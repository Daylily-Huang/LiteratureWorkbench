import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { OcrTextLayer } from "./OcrTextLayer";


afterEach(cleanup);


const blocks = [
  {
    block_id: "0:0",
    kind: "text" as const,
    text: "Selectable OCR text",
    bbox_1000: [100, 200, 600, 300] as [number, number, number, number],
    source: "mineru_ocr" as const,
    confidence: 0.85,
  },
  {
    block_id: "0:1",
    kind: "image" as const,
    text: null,
    bbox_1000: [0, 0, 1000, 1000] as [number, number, number, number],
    source: "mineru_ocr" as const,
    confidence: null,
  },
];


describe("OcrTextLayer", () => {
  it("maps bbox_1000 into rendered dimensions and exposes selectable reliability labels", () => {
    render(
      <OcrTextLayer
        blocks={blocks}
        coordinateReliable
        parserName="mineru"
        renderedHeight={1200}
        renderedWidth={800}
      />,
    );

    const layer = screen.getByRole("group", { name: "OCR 文本层（坐标可靠）" });
    const text = screen.getByText("Selectable OCR text");
    expect(layer).toHaveAttribute("data-parser", "ocr");
    expect(text).toHaveAttribute("data-confidence", "0.85");
    expect(text).toHaveStyle({
      left: "80px",
      top: "240px",
      width: "400px",
      height: "120px",
    });
    expect(text).toHaveClass("ocr-text-layer__text");
    expect(screen.queryByText("image")).not.toBeInTheDocument();
  });

  it("suppresses exact OCR geometry when coordinates are unreliable or direct", () => {
    const { rerender } = render(
      <OcrTextLayer
        blocks={blocks}
        coordinateReliable={false}
        parserName="mineru"
        renderedHeight={600}
        renderedWidth={400}
      />,
    );
    expect(screen.queryByRole("group", { name: /OCR 文本层/ })).not.toBeInTheDocument();

    rerender(
      <OcrTextLayer
        blocks={blocks}
        coordinateReliable
        parserName="pymupdf"
        renderedHeight={600}
        renderedWidth={400}
      />,
    );
    expect(screen.queryByRole("group", { name: /OCR 文本层/ })).not.toBeInTheDocument();
  });
});
