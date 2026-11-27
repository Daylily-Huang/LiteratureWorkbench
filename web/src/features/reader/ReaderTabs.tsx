import { useEffect, useRef } from "react";

export type ReaderTab = { documentId: string; title: string; scale: number };
const STORAGE_KEY = "lw-reader-tabs-v1";

// Tabs belong to this browser window. Reading progress remains in the workspace database.
export function readReaderTabs(documentId: string | null): ReaderTab[] {
  let tabs: ReaderTab[] = [];
  try {
    const stored: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "[]");
    if (Array.isArray(stored)) {
      const seen = new Set<string>();
      for (const item of stored) {
        if (!item || typeof item.documentId !== "string" || !item.documentId || seen.has(item.documentId)) continue;
        seen.add(item.documentId);
        tabs.push({ documentId: item.documentId, title: typeof item.title === "string" ? item.title : "文献",
          scale: typeof item.scale === "number" && Number.isFinite(item.scale) && item.scale >= .25 && item.scale <= 4 ? item.scale : 1 });
      }
    }
  } catch { /* Reading still works when browser storage is unavailable. */ }
  if (documentId && !tabs.some(tab => tab.documentId === documentId)) {
    tabs = [...tabs, { documentId, title: "正在读取文献…", scale: 1 }];
  }
  return tabs;
}

export function saveReaderTabs(tabs: ReaderTab[]) {
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(tabs)); } catch { /* Keep the in-memory session. */ }
}

type Props = {
  tabs: ReaderTab[];
  activeId: string | null;
  pages: Record<string, { page: number }>;
  canReopen: boolean;
  onOpen(id: string): void;
  onClose(id: string): void;
  onAdd(): void;
  onReopen(): void;
  onCompare?(id: string): void;
};

export function ReaderTabs({ tabs, activeId, pages, canReopen, onOpen, onClose, onAdd, onReopen, onCompare }: Props) {
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const container = list.current;
    if (!container) return;
    const reveal = () => {
      const selected = container.querySelector('[aria-selected="true"]')?.parentElement;
      if (!selected) return;
      const bounds = container.getBoundingClientRect();
      const tab = selected.getBoundingClientRect();
      if (tab.right > bounds.right) container.scrollLeft += tab.right - bounds.right;
      else if (tab.left < bounds.left) container.scrollLeft += tab.left - bounds.left;
    };
    reveal();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(reveal);
    observer?.observe(container);
    return () => observer?.disconnect();
  }, [activeId, tabs.length]);
  if (!tabs.length && !canReopen) return null;
  return <div className="reader-tabs-bar">
    <div className="reader-tabs" role="tablist" aria-label="已打开的文献" ref={list}>
      {tabs.map((tab, index) => <div className="reader-tab" data-active={tab.documentId === activeId} key={tab.documentId}>
        <button type="button" role="tab" aria-selected={tab.documentId === activeId}
          id={`reader-tab-${tab.documentId}`} aria-controls="reader-tab-panel"
          tabIndex={tab.documentId === activeId || (!activeId && index === 0) ? 0 : -1}
          title={`${tab.title} · 第 ${pages[tab.documentId]?.page ?? 1} 页`}
          onClick={() => onOpen(tab.documentId)} onKeyDown={event => {
            let next = index;
            if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
            else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
            else if (event.key === "Home") next = 0;
            else if (event.key === "End") next = tabs.length - 1;
            else if (event.key === "Delete") {
              event.preventDefault(); onClose(tab.documentId);
              const neighbor = list.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index + 1] ?? list.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[index - 1];
              neighbor?.focus(); return;
            } else return;
            event.preventDefault(); onOpen(tabs[next]!.documentId);
            list.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
          }}>
          <span className="reader-tab__icon" aria-hidden="true">PDF</span>
          <span className="reader-tab__title">{tab.title}</span>
          <span className="reader-tab__page" aria-hidden="true">p.{pages[tab.documentId]?.page ?? 1}</span>
        </button>
        {onCompare && tab.documentId !== activeId && <button type="button" className="reader-tab__compare" aria-label={`右侧对照 ${tab.title}`} title="放到右侧并排阅读" onClick={() => onCompare(tab.documentId)}>⇥</button>}
        <button type="button" className="reader-tab__close" aria-label={`关闭标签 ${tab.title}`} title="关闭标签（保留文献）" onClick={() => onClose(tab.documentId)}>×</button>
      </div>)}
    </div>
    <div className="reader-tabs__actions">
      <button type="button" onClick={onAdd} title="从文献总览选择多篇打开" aria-label="添加阅读标签">＋</button>
      {canReopen && <button type="button" onClick={onReopen} title="重新打开最近关闭的标签" aria-label="恢复关闭的标签">↶</button>}
    </div>
  </div>;
}
