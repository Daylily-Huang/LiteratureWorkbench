import { useEffect, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";

type OutlineItem = Awaited<ReturnType<PDFDocumentProxy["getOutline"]>>[number];
export function PdfOutline({ pdf, onJump, onClose }: { pdf: PDFDocumentProxy; onJump(page: number): void; onClose(): void }) {
  const [items, setItems] = useState<OutlineItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setLoading(true); setError("");
    void pdf.getOutline().then(value => { if (active) setItems(value ?? []); }).catch(() => { if (active) setError("目录暂时无法读取。"); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [pdf]);
  async function jump(item: OutlineItem) {
    try {
      const dest = typeof item.dest === "string" ? await pdf.getDestination(item.dest) : item.dest;
      if (!Array.isArray(dest) || !dest.length) throw new Error("missing_destination");
      const index = typeof dest[0] === "number" ? dest[0] : await pdf.getPageIndex(dest[0]);
      if (!Number.isInteger(index) || index < 0 || index >= pdf.numPages) throw new Error("invalid_page");
      onJump(index + 1); setError("");
    } catch { setError("这个目录项无法定位到文内页码。"); }
  }
  function render(nodes: OutlineItem[], depth = 0) {
    return nodes.map((item, index) => <li key={index}><button type="button" style={{ paddingLeft: `${12 + Math.min(depth, 5) * 12}px` }} onClick={() => void jump(item)} disabled={!item.dest}>{item.title || "未命名章节"}</button>{item.items?.length > 0 && <ul>{render(item.items, depth + 1)}</ul>}</li>);
  }
  return <div className="pdf-outline" aria-label="文献目录"><header><strong>文献目录</strong><button type="button" onClick={onClose}>关闭目录</button></header>{loading && <p role="status">正在读取目录…</p>}{error && <p role="alert">{error}</p>}{!loading && !items.length && !error && <p>这份 PDF 没有内嵌目录，可使用页码或正文查找定位。</p>}<ul>{render(items)}</ul></div>;
}
