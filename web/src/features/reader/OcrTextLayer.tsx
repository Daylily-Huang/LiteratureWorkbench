export type OcrBlock = {
  block_id: string;
  kind: "text" | "title" | "formula" | "image" | "table" | "caption";
  text: string | null;
  bbox_1000: [number, number, number, number];
  source: "pymupdf" | "mineru_ocr";
  confidence: number | null;
};


type OcrTextLayerProps = {
  parserName: "pymupdf" | "mineru";
  coordinateReliable: boolean;
  blocks: OcrBlock[];
  renderedWidth: number;
  renderedHeight: number;
};


export function OcrTextLayer({
  parserName,
  coordinateReliable,
  blocks,
  renderedWidth,
  renderedHeight,
}: OcrTextLayerProps) {
  if (parserName !== "mineru") return null;
  const label = coordinateReliable
    ? "OCR 文本层（坐标可靠）"
    : "OCR 文本层（坐标不可靠）";
  return (
    <div
      aria-label={label}
      className="textLayer ocr-text-layer"
      data-coordinate-reliable={String(coordinateReliable)}
      data-parser="ocr"
      role="group"
      style={{ height: `${renderedHeight}px`, width: `${renderedWidth}px` }}
    >
      {blocks.map((block) => {
        if (block.text === null || block.text === "") return null;
        const [x0, y0, x1, y1] = block.bbox_1000;
        const left = x0 / 1000 * renderedWidth;
        const top = y0 / 1000 * renderedHeight;
        const width = (x1 - x0) / 1000 * renderedWidth;
        const height = (y1 - y0) / 1000 * renderedHeight;
        return (
          <span
            className="ocr-text-layer__text"
            data-confidence={block.confidence ?? undefined}
            data-source="ocr"
            key={block.block_id}
            style={{
              fontSize: `${Math.max(1, height)}px`,
              height: `${height}px`,
              left: `${left}px`,
              lineHeight: 1,
              top: `${top}px`,
              width: `${width}px`,
            }}
          >
            {block.text}
          </span>
        );
      })}
    </div>
  );
}
