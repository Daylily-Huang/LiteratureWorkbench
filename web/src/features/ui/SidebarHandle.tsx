import { useRef, useState } from "react";

type Props = {
  side: "library" | "assistant";
  expanded: boolean;
  width: number;
  min: number;
  max: number;
  onToggle(): void;
  onResize(width: number): void;
  onReset(): void;
};

export function SidebarHandle({ side, expanded, width, min, max, onToggle, onResize, onReset }: Props) {
  const name = side === "library" ? "文献库" : "研究助手";
  const direction = side === "library" ? 1 : -1;
  const drag = useRef<{ pointer: number; x: number; width: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const change = (next: number) => onResize(Math.max(min, Math.min(max, next)));
  return <div className={`sidebar-handle sidebar-handle--${side}`} data-dragging={dragging}>
    <button type="button" className="sidebar-handle__toggle" aria-label={`${expanded ? "收起" : "展开"}${name}`} aria-expanded={expanded}
      title={`${expanded ? "收起" : "展开"}${name}`} onClick={onToggle}>
      <span aria-hidden="true">{(side === "library") === expanded ? "‹" : "›"}</span>
    </button>
    {expanded ? <div className="sidebar-handle__resize" role="separator" aria-label={`调整${name}宽度`}
      aria-orientation="vertical" aria-valuemin={min} aria-valuemax={max} aria-valuenow={Math.round(width)}
      aria-valuetext={`${Math.round(width)} 像素`} tabIndex={0} title="拖动调整宽度；双击恢复默认；方向键微调"
      onDoubleClick={onReset}
      onKeyDown={event => {
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault(); change(width + (event.key === "ArrowRight" ? 20 : -20) * direction);
        } else if (event.key === "Home" || event.key === "End") {
          event.preventDefault(); change(event.key === "Home" ? min : max);
        }
      }}
      onPointerDown={event => {
        if (event.button !== 0) return;
        event.preventDefault();
        drag.current = { pointer: event.pointerId, x: event.clientX, width };
        event.currentTarget.setPointerCapture(event.pointerId); setDragging(true);
      }}
      onPointerMove={event => {
        if (drag.current?.pointer !== event.pointerId) return;
        change(drag.current.width + (event.clientX - drag.current.x) * direction);
      }}
      onPointerUp={event => {
        if (drag.current?.pointer !== event.pointerId) return;
        drag.current = null; setDragging(false);
        event.currentTarget.releasePointerCapture(event.pointerId);
      }}
      onPointerCancel={() => { drag.current = null; setDragging(false); }}
      onLostPointerCapture={() => { drag.current = null; setDragging(false); }}
    ><span aria-hidden="true">⋮</span></div>
      : <span className="sidebar-handle__label" aria-hidden="true">{name}</span>}
  </div>;
}
