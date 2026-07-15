export type SelectedText = {
  documentId: string;
  documentVersionId: string;
  contentId: string;
  pdfPage: number;
  text: string;
  contextBefore: string;
  contextAfter: string;
  rects: Array<{ x: number; y: number; width: number; height: number }>;
  buttonAnchor: { x: number; y: number };
};

export type SelectionMetadata = Pick<
  SelectedText,
  "documentId" | "documentVersionId" | "contentId"
>;

export type SelectionCapture = {
  selected: SelectedText | null;
  hint: string | null;
};


const MAX_SELECTED_CODE_POINTS = 8000;
const MAX_CONTEXT_CODE_POINTS = 500;


function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/gu, " ").trim();
}


function capCodePoints(value: string, maximum: number): string {
  return Array.from(value).slice(0, maximum).join("");
}


function containingElement(node: Node): Element | null {
  return node instanceof Element ? node : node.parentElement;
}


function contextAroundRange(
  textLayer: HTMLElement,
  range: Range,
): { before: string; after: string } {
  const beforeRange = textLayer.ownerDocument.createRange();
  beforeRange.selectNodeContents(textLayer);
  beforeRange.setEnd(range.startContainer, range.startOffset);
  const afterRange = textLayer.ownerDocument.createRange();
  afterRange.selectNodeContents(textLayer);
  afterRange.setStart(range.endContainer, range.endOffset);
  const before = Array.from(normalizeWhitespace(beforeRange.toString()))
    .slice(-MAX_CONTEXT_CODE_POINTS)
    .join("");
  const after = capCodePoints(
    normalizeWhitespace(afterRange.toString()),
    MAX_CONTEXT_CODE_POINTS,
  );
  return {
    before,
    after,
  };
}


export function captureSamePageSelection(
  selection: Selection | null,
  visibleRoot: HTMLElement,
  metadata: SelectionMetadata,
  scale: number,
): SelectionCapture {
  if (
    selection === null
    || selection.rangeCount !== 1
    || selection.isCollapsed
    || scale <= 0
  ) {
    return { selected: null, hint: null };
  }
  const normalized = normalizeWhitespace(selection.toString());
  if (normalized === "") return { selected: null, hint: null };
  const range = selection.getRangeAt(0);
  if (range.collapsed) return { selected: null, hint: null };
  const start = containingElement(range.startContainer);
  const end = containingElement(range.endContainer);
  const startLayer = start?.closest<HTMLElement>(".textLayer") ?? null;
  const endLayer = end?.closest<HTMLElement>(".textLayer") ?? null;
  const startPage = start?.closest<HTMLElement>("[data-page-number]") ?? null;
  const endPage = end?.closest<HTMLElement>("[data-page-number]") ?? null;
  if (
    startLayer === null
    || endLayer === null
    || startLayer !== endLayer
    || startPage === null
    || endPage === null
    || startPage !== endPage
    || !visibleRoot.contains(startPage)
  ) {
    return { selected: null, hint: "请选择同一页文字" };
  }
  const pdfPage = Number(startPage.dataset.pageNumber);
  const pageBounds = startPage.getBoundingClientRect();
  const rects = Array.from(range.getClientRects())
    .filter((rect) => rect.width > 0 && rect.height > 0)
    .map((rect) => ({
      x: (rect.left - pageBounds.left) / scale,
      y: (rect.top - pageBounds.top) / scale,
      width: rect.width / scale,
      height: rect.height / scale,
    }));
  if (!Number.isInteger(pdfPage) || pdfPage < 1 || rects.length === 0) {
    return { selected: null, hint: "请选择同一页文字" };
  }
  const fullSelection = normalized;
  const text = capCodePoints(fullSelection, MAX_SELECTED_CODE_POINTS);
  const context = contextAroundRange(startLayer, range);
  const last = rects[rects.length - 1];
  if (last === undefined) return { selected: null, hint: "请选择同一页文字" };
  return {
    selected: {
      ...metadata,
      pdfPage,
      text,
      contextBefore: context.before,
      contextAfter: context.after,
      rects,
      buttonAnchor: { x: last.x + last.width, y: last.y + last.height },
    },
    hint: null,
  };
}
