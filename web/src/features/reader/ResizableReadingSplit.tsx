import { useRef, useState, type ReactNode } from "react";

export function ResizableReadingSplit({ left, right, activeSide = "left", onActivate }: { left: ReactNode; right: ReactNode; activeSide?: "left" | "right"; onActivate?(side: "left" | "right"): void }) {
  const root = useRef<HTMLDivElement>(null);
  const pointer = useRef<number | null>(null);
  const [ratio, setRatio] = useState(50);
  const resize = (value: number) => setRatio(Math.max(25, Math.min(75, value)));
  return <div className="reading-split" ref={root} style={{ gridTemplateColumns: `minmax(0, ${ratio}fr) 14px minmax(0, ${100 - ratio}fr)` }}>
    <section className={`reading-split__pane${activeSide === "left" ? " reading-split__pane--active" : ""}`} aria-label="左侧文献" onPointerDownCapture={() => onActivate?.("left")} onFocusCapture={() => onActivate?.("left")}>{left}</section>
    <div className="reading-split__divider" role="separator" aria-label="调整两篇文献宽度" aria-orientation="vertical"
      aria-valuemin={25} aria-valuemax={75} aria-valuenow={Math.round(ratio)} aria-valuetext={`左侧 ${Math.round(ratio)}%`}
      title="拖动调整两篇宽度，双击恢复均分" tabIndex={0} onDoubleClick={() => setRatio(50)}
      onPointerDown={event => { if (event.button !== 0) return; event.preventDefault(); pointer.current = event.pointerId; event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={event => {
        if (pointer.current !== event.pointerId || !root.current) return;
        const bounds = root.current.getBoundingClientRect();
        if (bounds.width > 14) resize((event.clientX - bounds.left - 7) / (bounds.width - 14) * 100);
      }}
      onPointerUp={event => { if (pointer.current !== event.pointerId) return; pointer.current = null; event.currentTarget.releasePointerCapture(event.pointerId); }}
      onPointerCancel={() => { pointer.current = null; }} onLostPointerCapture={() => { pointer.current = null; }}
      onKeyDown={event => {
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); resize(ratio + (event.key === "ArrowRight" ? 5 : -5)); }
        if (event.key === "Home" || event.key === "End") { event.preventDefault(); resize(event.key === "Home" ? 25 : 75); }
      }}><span aria-hidden="true">⋮</span></div>
    <section className={`reading-split__pane${activeSide === "right" ? " reading-split__pane--active" : ""}`} aria-label="右侧文献" onPointerDownCapture={() => onActivate?.("right")} onFocusCapture={() => onActivate?.("right")}>{right}</section>
  </div>;
}
