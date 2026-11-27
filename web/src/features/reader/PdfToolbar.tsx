import { useEffect, useState } from "react";

type PdfToolbarProps = {
  onOutline?(): void;
  outlineOpen?: boolean;
  onSearch?(): void;
  searchOpen?: boolean;
  currentPage: number;
  totalPages: number;
  scale: number;
  onPageChange(page: number): void;
  onScaleChange(scale: number): void;
  onFitWidth(): void;
};


export function PdfToolbar({
  onOutline,
  outlineOpen,
  onSearch,
  searchOpen = false,
  currentPage,
  totalPages,
  scale,
  onPageChange,
  onScaleChange,
  onFitWidth,
}: PdfToolbarProps) {
  const [pageDraft, setPageDraft] = useState(String(currentPage));
  useEffect(() => setPageDraft(String(currentPage)), [currentPage]);
  function commitPage() {
    setPage(Number(pageDraft));
    if (!Number.isInteger(Number(pageDraft)) || Number(pageDraft) < 1 || Number(pageDraft) > totalPages) setPageDraft(String(currentPage));
  }

  function setPage(value: number) {
    if (Number.isInteger(value) && value >= 1 && value <= totalPages) {
      onPageChange(value);
    }
  }

  return (
    <div aria-label="PDF 工具栏" className="pdf-toolbar" role="toolbar">
      <div className="pdf-toolbar__group">
        <button
          aria-label="上一页"
          className="pdf-toolbar__btn"
          disabled={currentPage <= 1}
          onClick={() => setPage(currentPage - 1)}
          type="button"
        >
          上一页
        </button>
        <label className="pdf-toolbar__label">
          页码
          <input
            aria-label="PDF 页码"
            className="pdf-toolbar__input"
            max={totalPages}
            min={1}
            onChange={(event) => setPageDraft(event.target.value)}
            onBlur={commitPage}
            onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); commitPage(); } }}
            type="number"
            value={pageDraft}
          />
        </label>
        <span className="pdf-toolbar__page-display">{currentPage} / {totalPages}</span>
        <button
          aria-label="下一页"
          className="pdf-toolbar__btn"
          disabled={currentPage >= totalPages}
          onClick={() => setPage(currentPage + 1)}
          type="button"
        >
          下一页
        </button>
      </div>

      {onOutline && <button className="pdf-toolbar__btn" aria-label="打开文献目录" aria-expanded={outlineOpen} onClick={onOutline} type="button">目录</button>}
      {onSearch && <button className="pdf-toolbar__btn" aria-label="查找正文" aria-expanded={searchOpen} onClick={onSearch} type="button">查找</button>}
      <div className="pdf-toolbar__separator" />

      <div className="pdf-toolbar__group">
        <button
          aria-label="缩小"
          className="pdf-toolbar__btn"
          disabled={scale <= 0.25}
          onClick={() => onScaleChange(Math.max(0.25, scale - 0.25))}
          type="button"
        >
          缩小
        </button>
        <button
          aria-label="放大"
          className="pdf-toolbar__btn"
          disabled={scale >= 4}
          onClick={() => onScaleChange(Math.min(4, scale + 0.25))}
          type="button"
        >
          放大
        </button>
      </div>

      <div className="pdf-toolbar__separator" />

      <div className="pdf-toolbar__group">
        <button
          aria-label="适合宽度"
          className="pdf-toolbar__btn"
          onClick={onFitWidth}
          type="button"
        >
          适合宽度
        </button>
        <button
          aria-label="重置缩放"
          className="pdf-toolbar__btn"
          disabled={scale === 1}
          onClick={() => onScaleChange(1)}
          type="button"
        >
          重置缩放
        </button>
        <output aria-label="缩放比例" className="pdf-toolbar__scale">{Math.round(scale * 100)}%</output>
      </div>
    </div>
  );
}
