import { LibraryPanel } from "../features/library/LibraryPanel";

export function Workbench() {
  return (
    <div className="workbench">
      <aside aria-label="文献库" className="workbench__library">
        <LibraryPanel />
      </aside>
      <main aria-label="PDF 阅读器" className="workbench__reader">
        <h2>PDF 阅读器</h2>
      </main>
      <aside aria-label="研究助手" className="workbench__assistant">
        <h2>研究助手</h2>
      </aside>
    </div>
  );
}
