/**
 * Renders the source fragments of a research answer citation directly on
 * the PDF page.  Rects arrive as parser ``bbox_1000`` coordinates (0–1000
 * page-relative grid), the same space the OCR text layer uses, so the
 * highlight lands exactly on the contributing blocks.
 */


type CitationHighlightProps = {
  rects: number[][];
  renderedWidth: number;
  renderedHeight: number;
  /** Bumped per jump so clicking the same citation re-plays the flash. */
  nonce: number;
};


export function CitationHighlight({
  rects,
  renderedWidth,
  renderedHeight,
  nonce,
}: CitationHighlightProps) {
  if (rects.length === 0 || renderedWidth <= 0 || renderedHeight <= 0) return null;
  return (
    <div aria-label="引用来源高亮" className="citation-highlight" key={nonce}>
      {rects.map((rect, index) => {
        const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = rect;
        return (
          <span
            className="citation-highlight__rect"
            data-testid="citation-highlight-rect"
            key={`${nonce}:${index}`}
            style={{
              left: `${(x0 / 1000) * renderedWidth}px`,
              top: `${(y0 / 1000) * renderedHeight}px`,
              width: `${((x1 - x0) / 1000) * renderedWidth}px`,
              height: `${((y1 - y0) / 1000) * renderedHeight}px`,
            }}
          />
        );
      })}
    </div>
  );
}
