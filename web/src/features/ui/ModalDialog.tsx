import { useEffect, useRef, type ReactNode } from "react";

type ModalDialogProps = { children: ReactNode; label: string; onClose(): void };
const FOCUSABLE = ["button:not([disabled])", "input:not([disabled])", "select:not([disabled])", "textarea:not([disabled])", "[href]", '[tabindex]:not([tabindex="-1"])'].join(",");

export function ModalDialog({ children, label, onClose }: ModalDialogProps) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const root = panel.current;
    root?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
    function keydown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab" || root === null) return;
      const items = [...root.querySelectorAll<HTMLElement>(FOCUSABLE)];
      const first = items[0];
      const last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener("keydown", keydown);
    return () => { document.removeEventListener("keydown", keydown); previous?.focus(); };
  }, [onClose]);
  return (
    <div className="modal-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div aria-label={label} aria-modal="true" className="modal-dialog" ref={panel} role="dialog">{children}</div>
    </div>
  );
}
