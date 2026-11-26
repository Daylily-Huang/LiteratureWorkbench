import { createPortal } from "react-dom";
import { useEffect, useRef, type ReactNode } from "react";

type ModalDialogProps = { children: ReactNode; label: string; onClose(): void };
const FOCUSABLE = ["button:not([disabled])", "input:not([disabled])", "select:not([disabled])", "textarea:not([disabled])", "[href]", '[tabindex]:not([tabindex="-1"])'].join(",");
const panels: HTMLDivElement[] = [];
function controls(root: HTMLElement) {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(node => node.tabIndex >= 0 && !node.closest('[hidden], [inert]') && getComputedStyle(node).display !== "none" && getComputedStyle(node).visibility !== "hidden");
}
function focusFirst(root: HTMLElement) {
  (root.querySelector<HTMLElement>('[data-autofocus]') ?? controls(root)[0] ?? root).focus();
}
function syncLayers() {
  panels.forEach(root => { const scrim = root.parentElement; if (scrim) scrim.inert = root !== panels.at(-1); });
}

export function ModalDialog({ children, label, onClose }: ModalDialogProps) {
  const panel = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const root = panel.current;
    if (!root) return;
    panels.push(root); syncLayers(); focusFirst(root);
    function keydown(event: KeyboardEvent) {
      if (panels.at(-1) !== root || event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") { event.preventDefault(); onCloseRef.current(); return; }
      if (event.key !== "Tab" || root === null) return;
      const items = controls(root);
      const first = items[0];
      const last = items.at(-1);
      if (!first) { event.preventDefault(); root.focus(); return; }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    function focusin(event: FocusEvent) {
      if (panels.at(-1) === root && event.target instanceof Node && !root.contains(event.target)) focusFirst(root);
    }
    document.addEventListener("keydown", keydown);
    document.addEventListener("focusin", focusin);
    return () => {
      const wasTop = panels.at(-1) === root;
      const index = panels.indexOf(root); if (index >= 0) panels.splice(index, 1);
      document.removeEventListener("keydown", keydown); document.removeEventListener("focusin", focusin);
      syncLayers();
      if (wasTop) {
        const top = panels.at(-1);
        if (previous?.isConnected && (!top || top.contains(previous))) previous.focus();
        else if (top) focusFirst(top);
      }
    };
  }, []);
  return createPortal(
    <div className="modal-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div aria-label={label} aria-modal="true" className="modal-dialog" ref={panel} role="dialog" tabIndex={-1}>{children}</div>
    </div>,
    document.querySelector(".workbench-shell") ?? document.body,
  );
}
