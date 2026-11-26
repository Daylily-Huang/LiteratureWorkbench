import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";

type Hit = { page: number; before: string; match: string; after: string };
export function PdfSearch({ pdf, onJump, onClose, initialQuery = "" }: {
  pdf: PDFDocumentProxy; onJump(page: number): void; onClose(): void; initialQuery?: string;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [hits, setHits] = useState<Hit[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const cache = useRef(new Map<number, string>());
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => {
    let stopped = false;
    const needle = query.trim().toLocaleLowerCase();
    setHits([]); setError(false); setBusy(Boolean(needle));
    if (!needle) return;
    const timer = window.setTimeout(() => {
      void (async () => {
        const found: Hit[] = [];
        try {
          for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber++) {
            if (stopped) return;
            let text = cache.current.get(pageNumber);
            if (text === undefined) {
              const page = await pdf.getPage(pageNumber);
              if (stopped) return;
              const content = await page.getTextContent();
              if (stopped) return;
              text = content.items.map(item => "str" in item ? item.str : "").join(" ").replace(/\s+/g, " ");
              cache.current.set(pageNumber, text);
            }
            const index = text.toLocaleLowerCase().indexOf(needle);
            if (index >= 0) found.push({ page: pageNumber, before: text.slice(Math.max(0, index - 45), index), match: text.slice(index, index + needle.length), after: text.slice(index + needle.length, index + needle.length + 80) });
            if (found.length >= 100) break;
          }
          if (!stopped) setHits(found);
        } catch { if (!stopped) setError(true); }
        finally { if (!stopped) setBusy(false); }
      })();
    }, 250);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [query, pdf]);
  return <section className="pdf-search" aria-label="正文查找" onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}>
    <div className="pdf-search__input"><input ref={input} aria-label="查找正文内容" placeholder="输入关键词，查找整篇 PDF" value={query} onChange={event => setQuery(event.target.value)} /><button type="button" className="icon-button" aria-label="关闭正文查找" onClick={onClose}>×</button></div>
    <p role="status">{busy ? "正在查找全文…" : error ? "部分页面无法读取，请重试查找。" : !query.trim() ? "搜索 PDF 文字层；扫描页需先完成文字识别。" : hits.length ? `找到 ${hits.length === 100 ? "至少 " : ""}${hits.length} 个匹配页，点击返回原文` : "没有找到匹配内容；扫描页可能没有可搜索的文字。"}</p>
    <div className="pdf-search__results">{hits.map(hit => <button key={hit.page} type="button" onClick={() => onJump(hit.page)}><strong>第 {hit.page} 页</strong><span>{hit.before}<mark>{hit.match}</mark>{hit.after}</span></button>)}</div>
  </section>;
}
