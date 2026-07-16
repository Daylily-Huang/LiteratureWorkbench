import type { PdfAnnotation } from "./annotations";


type AnnotationOverlayProps = {
  annotations: PdfAnnotation[];
  pageTransform?: number[];
  activeAnnotationId?: string | null;
  onOpen?(annotationId: string): void;
};


function pdfPointToViewport(
  x: number,
  y: number,
  transform: number[],
): { x: number; y: number } {
  const [a = 1, b = 0, c = 0, d = 1, e = 0, f = 0] = transform;
  return { x: a * x + c * y + e, y: b * x + d * y + f };
}


export function AnnotationOverlay({
  annotations,
  pageTransform,
  activeAnnotationId,
  onOpen,
}: AnnotationOverlayProps) {
  if (pageTransform === undefined) return null;
  return (
    <div className="annotation-overlay">
      {annotations.flatMap((annotation) => annotation.quads.map((quad, index) => {
        const points = Array.from({ length: 4 }, (_, pointIndex) => pdfPointToViewport(
          quad[pointIndex * 2] ?? 0,
          quad[pointIndex * 2 + 1] ?? 0,
          pageTransform,
        ));
        const left = Math.min(...points.map((point) => point.x));
        const top = Math.min(...points.map((point) => point.y));
        const right = Math.max(...points.map((point) => point.x));
        const bottom = Math.max(...points.map((point) => point.y));
        return (
          <button
            aria-label={`${annotation.read_only ? "只读" : "打开"}批注：${annotation.selected_text || annotation.comment || annotation.type}`}
            className={`annotation-overlay__item${annotation.annotation_id === activeAnnotationId ? " annotation-overlay__item--active" : ""}`}
            key={`${annotation.annotation_id}:${index}`}
            onClick={() => onOpen?.(annotation.annotation_id)}
            style={{ left, top, width: right - left, height: bottom - top }}
            type="button"
          />
        );
      }))}
    </div>
  );
}
