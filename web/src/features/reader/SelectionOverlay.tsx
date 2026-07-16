import type { SelectedText } from "./selection";


type SelectionOverlayProps = {
  selected: SelectedText;
  scale: number;
  onTranslate?(): void;
};


export function SelectionOverlay({ selected, scale, onTranslate }: SelectionOverlayProps) {
  return (
    <div className="selection-overlay">
      {selected.rects.map((rect, index) => (
        <span
          className="selection-overlay__rect"
          data-testid="selection-rect"
          key={`${rect.x}:${rect.y}:${index}`}
          style={{
            left: rect.x * scale,
            top: rect.y * scale,
            width: rect.width * scale,
            height: rect.height * scale,
          }}
        />
      ))}
      <button
        aria-label="翻译所选文字"
        className="selection-overlay__button"
        disabled={!selected.translationEligible}
        onClick={onTranslate}
        style={{
          left: selected.buttonAnchor.x * scale,
          top: selected.buttonAnchor.y * scale,
        }}
        type="button"
      >
        {selected.translationEligible ? "翻译" : "超过 8,000 字"}
      </button>
    </div>
  );
}
