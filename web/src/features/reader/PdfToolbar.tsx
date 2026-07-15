type PdfToolbarProps = {
  currentPage: number;
  totalPages: number;
  scale: number;
  onPageChange(page: number): void;
  onScaleChange(scale: number): void;
  onFitWidth(): void;
};


export function PdfToolbar({
  currentPage,
  totalPages,
  scale,
  onPageChange,
  onScaleChange,
  onFitWidth,
}: PdfToolbarProps) {
  function setPage(value: number) {
    if (Number.isInteger(value) && value >= 1 && value <= totalPages) {
      onPageChange(value);
    }
  }

  return (
    <div aria-label="PDF 工具栏" className="pdf-toolbar" role="toolbar">
      <button
        aria-label="上一页"
        disabled={currentPage <= 1}
        onClick={() => setPage(currentPage - 1)}
        type="button"
      >
        上一页
      </button>
      <label>
        页码
        <input
          aria-label="PDF 页码"
          max={totalPages}
          min={1}
          onChange={(event) => setPage(Number(event.target.value))}
          type="number"
          value={currentPage}
        />
      </label>
      <span>{currentPage} / {totalPages}</span>
      <button
        aria-label="下一页"
        disabled={currentPage >= totalPages}
        onClick={() => setPage(currentPage + 1)}
        type="button"
      >
        下一页
      </button>
      <button
        aria-label="缩小"
        disabled={scale <= 0.25}
        onClick={() => onScaleChange(Math.max(0.25, scale - 0.25))}
        type="button"
      >
        缩小
      </button>
      <button
        aria-label="放大"
        disabled={scale >= 4}
        onClick={() => onScaleChange(Math.min(4, scale + 0.25))}
        type="button"
      >
        放大
      </button>
      <button aria-label="适合宽度" onClick={onFitWidth} type="button">
        适合宽度
      </button>
      <button
        aria-label="重置缩放"
        disabled={scale === 1}
        onClick={() => onScaleChange(1)}
        type="button"
      >
        重置缩放
      </button>
      <output aria-label="缩放比例">{Math.round(scale * 100)}%</output>
    </div>
  );
}
