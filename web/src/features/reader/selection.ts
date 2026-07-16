export type SelectedText = {
  documentId: string;
  documentVersionId: string;
  contentId: string;
  pdfPage: number;
  text: string;
  contextBefore: string;
  contextAfter: string;
  rects: Array<{ x: number; y: number; width: number; height: number }>;
  pdfQuads: number[][];
  buttonAnchor: { x: number; y: number };
  viewportAnchor: { x: number; y: number };
  translationEligible: boolean;
};

export type SelectionMetadata = Pick<
  SelectedText,
  "documentId" | "documentVersionId" | "contentId"
>;

export type SelectionCapture = {
  selected: SelectedText | null;
  hint: string | null;
};


export const MAX_TRANSLATION_CODE_POINTS = 8000;
const MAX_VISIBLE_RECTS = 200;
const MAX_CONTEXT_CODE_POINTS = 500;
const SELECTION_HINT = "请在同一栏选择一句或一段";


function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/gu, " ").trim();
}


function capCodePoints(value: string, maximum: number): string {
  return Array.from(value).slice(0, maximum).join("");
}


function containingElement(node: Node): Element | null {
  return node instanceof Element ? node : node.parentElement;
}


export function viewportPointToPdfPoint(
  point: { x: number; y: number },
  transform: [number, number, number, number, number, number],
): { x: number; y: number } {
  const [a, b, c, d, e, f] = transform;
  const determinant = a * d - b * c;
  if (Math.abs(determinant) < Number.EPSILON) throw new Error("pdf_viewport_not_invertible");
  const translatedX = point.x - e;
  const translatedY = point.y - f;
  return {
    x: (d * translatedX - c * translatedY) / determinant,
    y: (-b * translatedX + a * translatedY) / determinant,
  };
}


function pageTransform(page: HTMLElement): [number, number, number, number, number, number] | null {
  const raw = page.dataset.pdfTransform;
  if (raw === undefined) return null;
  try {
    const value = JSON.parse(raw) as unknown;
    if (!Array.isArray(value) || value.length !== 6 || !value.every(Number.isFinite)) return null;
    return value as [number, number, number, number, number, number];
  } catch {
    return null;
  }
}


function spansColumns(rects: DOMRect[]): boolean {
  for (let leftIndex = 0; leftIndex < rects.length; leftIndex += 1) {
    const left = rects[leftIndex];
    if (left === undefined) continue;
    for (let rightIndex = leftIndex + 1; rightIndex < rects.length; rightIndex += 1) {
      const right = rects[rightIndex];
      if (right === undefined) continue;
      const verticalOverlap = Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top);
      const requiredOverlap = Math.min(left.height, right.height) * 0.5;
      if (verticalOverlap < requiredOverlap) continue;
      const horizontalGap = Math.max(left.left, right.left) - Math.min(left.right, right.right);
      const meaningfulGap = Math.max(48, Math.min(left.height, right.height) * 2);
      if (horizontalGap > meaningfulGap) return true;
    }
  }
  return false;
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
  options: { allowCrossColumn?: boolean } = {},
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
    return { selected: null, hint: SELECTION_HINT };
  }
  const pdfPage = Number(startPage.dataset.pageNumber);
  const pageBounds = startPage.getBoundingClientRect();
  const clientRects = Array.from(range.getClientRects())
    .filter((rect) => rect.width > 0 && rect.height > 0);
  if (
    clientRects.length > MAX_VISIBLE_RECTS
    || (!options.allowCrossColumn && spansColumns(clientRects))
  ) {
    return { selected: null, hint: SELECTION_HINT };
  }
  const rects = clientRects
    .map((rect) => ({
      x: (rect.left - pageBounds.left) / scale,
      y: (rect.top - pageBounds.top) / scale,
      width: rect.width / scale,
      height: rect.height / scale,
    }));
  const transform = pageTransform(startPage);
  const pdfQuads = transform === null ? [] : clientRects.map((rect) => {
    const left = rect.left - pageBounds.left;
    const right = rect.right - pageBounds.left;
    const top = rect.top - pageBounds.top;
    const bottom = rect.bottom - pageBounds.top;
    const points = [
      viewportPointToPdfPoint({ x: left, y: top }, transform),
      viewportPointToPdfPoint({ x: right, y: top }, transform),
      viewportPointToPdfPoint({ x: left, y: bottom }, transform),
      viewportPointToPdfPoint({ x: right, y: bottom }, transform),
    ];
    return points.flatMap((point) => [point.x, point.y]);
  });
  if (!Number.isInteger(pdfPage) || pdfPage < 1 || rects.length === 0) {
    return { selected: null, hint: SELECTION_HINT };
  }
  const context = contextAroundRange(startLayer, range);
  const last = rects[rects.length - 1];
  const lastClientRect = clientRects[clientRects.length - 1];
  if (last === undefined || lastClientRect === undefined) {
    return { selected: null, hint: SELECTION_HINT };
  }
  return {
    selected: {
      ...metadata,
      pdfPage,
      text: normalized,
      contextBefore: context.before,
      contextAfter: context.after,
      rects,
      pdfQuads,
      buttonAnchor: { x: last.x + last.width, y: last.y + last.height },
      viewportAnchor: { x: lastClientRect.right, y: lastClientRect.bottom },
      translationEligible: Array.from(normalized).length <= MAX_TRANSLATION_CODE_POINTS,
    },
    hint: null,
  };
}
