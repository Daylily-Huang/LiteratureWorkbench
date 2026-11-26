import { cleanPdfCopyText } from "./copyText";
import {
  MAX_TRANSLATION_CODE_POINTS,
  viewportPointToPdfPoint,
  type SelectionCapture,
  type SelectionMetadata,
} from "./selection";


const MAX_VISIBLE_RECTS = 1000;
const MAX_CONTEXT_CODE_POINTS = 500;
const SELECTION_HINT = "请在同一栏选择一句或一段";
// Same-line spans listed far apart in the content stream are column neighbours,
// not line mates. Out-of-order suffix spans observed in real PDFs sit within a
// few nodes of their line; two-column layouts put the other column dozens of
// nodes away.
const MAX_LINE_BRIDGE_DISTANCE = 8;


export type VisualFragmentSeed = {
  node: Text;
  bounds: DOMRect;
  measure(start: number, end: number): DOMRect[];
};

type VisualFragment = VisualFragmentSeed & {
  line: number;
  region: number;
};

export type VisualTextIndex = {
  fragments: VisualFragment[];
  byNode: Map<Text, VisualFragment>;
  lineBounds: Map<number, DOMRect>;
};

export type VisualTextHit = {
  node: Text;
  offset: number;
  clientX: number;
  clientY: number;
};

type ResolveOptions = {
  metadata: SelectionMetadata;
  page: number;
  pageBounds: DOMRect;
  transform: [number, number, number, number, number, number];
  scale: number;
  allowCrossRegion?: boolean;
};

export type VisualSelectionGesture = {
  index: VisualTextIndex;
  start: VisualTextHit;
  startCandidates: VisualTextHit[];
  options: Omit<ResolveOptions, "allowCrossRegion">;
  ownerDocument: Document;
};


function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/gu, " ").trim();
}


function rectDistance(rect: DOMRect, x: number, y: number): number {
  const dx = Math.max(rect.left - x, 0, x - rect.right);
  const dy = Math.max(rect.top - y, 0, y - rect.bottom);
  return Math.hypot(dx, dy);
}


function overlap(
  firstStart: number,
  firstEnd: number,
  secondStart: number,
  secondEnd: number,
): number {
  return Math.max(0, Math.min(firstEnd, secondEnd) - Math.max(firstStart, secondStart));
}


function shouldConnect(first: VisualFragmentSeed, second: VisualFragmentSeed): boolean {
  const a = first.bounds;
  const b = second.bounds;
  const minimumHeight = Math.max(1, Math.min(a.height, b.height));
  const maximumHeight = Math.max(a.height, b.height);
  const heightRatio = maximumHeight / minimumHeight;
  if (heightRatio > 1.8) return false;

  const verticalOverlap = overlap(a.top, a.bottom, b.top, b.bottom);
  if (verticalOverlap >= minimumHeight * 0.55) {
    const horizontalGap = Math.max(a.left, b.left) - Math.min(a.right, b.right);
    return horizontalGap >= -1 && horizontalGap <= Math.max(36, minimumHeight * 2.5);
  }

  const verticalGap = Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom);
  if (verticalGap > Math.max(48, maximumHeight * 4)) return false;
  const horizontalOverlap = overlap(a.left, a.right, b.left, b.right);
  const minimumWidth = Math.max(1, Math.min(a.width, b.width));
  const overlapRatio = horizontalOverlap / minimumWidth;
  const edgeTolerance = maximumHeight * 2.5;
  const aligned = (
    Math.abs(a.left - b.left) <= edgeTolerance
    || Math.abs(a.right - b.right) <= edgeTolerance
  );
  const centerDistance = Math.abs(
    (a.left + a.right) / 2 - (b.left + b.right) / 2,
  );
  return aligned || (overlapRatio >= 0.65 && centerDistance <= minimumWidth * 0.2);
}


function visualCompare(first: VisualFragment, second: VisualFragment): number {
  const tolerance = Math.max(2, Math.min(first.bounds.height, second.bounds.height) * 0.45);
  if (Math.abs(first.bounds.top - second.bounds.top) <= tolerance) {
    return first.bounds.left - second.bounds.left;
  }
  return first.bounds.top - second.bounds.top;
}


function sameVisualLine(
  first: VisualFragmentSeed,
  second: VisualFragmentSeed,
  sourceDistance: number,
): boolean {
  const a = first.bounds;
  const b = second.bounds;
  const minimumHeight = Math.max(1, Math.min(a.height, b.height));
  const verticalOverlap = overlap(a.top, a.bottom, b.top, b.bottom);
  if (verticalOverlap < minimumHeight * 0.55) return false;
  const horizontalGap = Math.max(a.left, b.left) - Math.min(a.right, b.right);
  if (horizontalGap > Math.max(36, minimumHeight * 2.5)) return false;
  // PDF content streams can list a right-side suffix after the next visual line.
  // A small non-overlapping gap is still unambiguous when the text nodes sit
  // close together in the content stream. Distant nodes with the same small
  // gap are almost always two physical columns separated by a narrow gutter
  // (justified text can end within a few pixels of the next column), so they
  // must never collapse into one visual line.
  if (horizontalGap >= -2) return sourceDistance <= MAX_LINE_BRIDGE_DISTANCE;
  if (sourceDistance !== 1) return false;

  // PDF.js can give adjacent styled spans bounds which overlap heavily because
  // font metrics are approximate. Only join that kind of overlap when the text
  // nodes are adjacent in the PDF content stream; otherwise two physical
  // columns whose wide bounds overlap would collapse into one visual line.
  const edgeTolerance = Math.max(2, minimumHeight * 0.15);
  const nested = (
    b.left >= a.left - edgeTolerance
    && b.right <= a.right + edgeTolerance
  ) || (
    a.left >= b.left - edgeTolerance
    && a.right <= b.right + edgeTolerance
  );
  return nested || Math.abs(a.left - b.left) <= Math.max(48, minimumHeight * 10);
}


function unionBounds(rects: DOMRect[]): DOMRect {
  const left = Math.min(...rects.map((rect) => rect.left));
  const top = Math.min(...rects.map((rect) => rect.top));
  const right = Math.max(...rects.map((rect) => rect.right));
  const bottom = Math.max(...rects.map((rect) => rect.bottom));
  return new DOMRect(left, top, right - left, bottom - top);
}


export function createVisualTextIndex(seeds: VisualFragmentSeed[]): VisualTextIndex {
  const parents = seeds.map((_, index) => index);
  const lineParents = seeds.map((_, index) => index);
  function find(index: number): number {
    let current = index;
    while (parents[current] !== current) {
      const parent = parents[current];
      if (parent === undefined) break;
      current = parent;
    }
    let cursor = index;
    while (parents[cursor] !== current) {
      const next = parents[cursor];
      if (next === undefined) break;
      parents[cursor] = current;
      cursor = next;
    }
    return current;
  }
  function union(first: number, second: number): void {
    const firstRoot = find(first);
    const secondRoot = find(second);
    if (firstRoot !== secondRoot) parents[secondRoot] = firstRoot;
  }
  function findLine(index: number): number {
    let current = index;
    while (lineParents[current] !== current) {
      const parent = lineParents[current];
      if (parent === undefined) break;
      current = parent;
    }
    return current;
  }
  function unionLine(first: number, second: number): void {
    const firstRoot = findLine(first);
    const secondRoot = findLine(second);
    if (firstRoot !== secondRoot) lineParents[secondRoot] = firstRoot;
  }
  for (let first = 0; first < seeds.length; first += 1) {
    for (let second = first + 1; second < seeds.length; second += 1) {
      const firstSeed = seeds[first];
      const secondSeed = seeds[second];
      if (firstSeed !== undefined && secondSeed !== undefined && shouldConnect(firstSeed, secondSeed)) {
        union(first, second);
      }
      if (
        firstSeed !== undefined
        && secondSeed !== undefined
        && sameVisualLine(firstSeed, secondSeed, second - first)
      ) {
        unionLine(first, second);
      }
    }
  }
  const regionIds = new Map<number, number>();
  const lineIds = new Map<number, number>();
  const fragments = seeds.map((seed, index) => {
    const root = find(index);
    let region = regionIds.get(root);
    if (region === undefined) {
      region = regionIds.size;
      regionIds.set(root, region);
    }
    const lineRoot = findLine(index);
    let line = lineIds.get(lineRoot);
    if (line === undefined) {
      line = lineIds.size;
      lineIds.set(lineRoot, line);
    }
    return { ...seed, line, region };
  });
  fragments.sort(visualCompare);
  const lineBounds = new Map<number, DOMRect>();
  for (const line of new Set(fragments.map((fragment) => fragment.line))) {
    lineBounds.set(
      line,
      unionBounds(
        fragments.filter((fragment) => fragment.line === line).map((fragment) => fragment.bounds),
      ),
    );
  }
  return {
    fragments,
    byNode: new Map(fragments.map((fragment) => [fragment.node, fragment])),
    lineBounds,
  };
}


function separator(first: VisualFragment, second: VisualFragment): string {
  const firstText = first.node.data;
  const secondText = second.node.data;
  if (/\s$/u.test(firstText) || /^\s/u.test(secondText)) return "";
  if (/^[,.;:!?%\)\]\}，。；：！？％、]/u.test(secondText)) return "";
  if (hasSourceWhitespaceBetween(first.node, second.node)) return " ";
  if (/[a-z]$/u.test(firstText) && /^[A-Z][a-z]/u.test(secondText)) return " ";
  if (first.line !== second.line) return " ";
  const verticalOverlap = overlap(
    first.bounds.top,
    first.bounds.bottom,
    second.bounds.top,
    second.bounds.bottom,
  );
  const sameLine = verticalOverlap >= Math.min(first.bounds.height, second.bounds.height) * 0.5;
  const gap = second.bounds.left - first.bounds.right;
  return sameLine && gap <= 1 ? "" : " ";
}


function hasSourceWhitespaceBetween(first: Text, second: Text): boolean {
  if (first.ownerDocument !== second.ownerDocument) return false;
  const following = first.ownerDocument.defaultView?.Node.DOCUMENT_POSITION_FOLLOWING ?? 4;
  if ((first.compareDocumentPosition(second) & following) === 0) return false;
  try {
    const range = first.ownerDocument.createRange();
    range.setStartAfter(first);
    range.setEndBefore(second);
    const between = range.toString();
    return between !== "" && /^\s+$/u.test(between);
  } catch {
    return false;
  }
}


function joinSlices(
  fragments: VisualFragment[],
  slices: string[],
): string {
  let value = "";
  for (let index = 0; index < slices.length; index += 1) {
    const text = slices[index] ?? "";
    if (text === "") continue;
    const previousIndex = index - 1;
    const previous = fragments[previousIndex];
    const current = fragments[index];
    if (value !== "" && previous !== undefined && current !== undefined) {
      value += separator(previous, current);
    }
    value += text;
  }
  return normalizeWhitespace(value);
}

function clipboardSlices(fragments: VisualFragment[], slices: string[]): string {
  let value = "";
  let previous: VisualFragment | undefined;
  for (let i = 0; i < fragments.length; i += 1) {
    const current = fragments[i]!;
    const slice = slices[i] ?? "";
    if (!slice) continue;
    if (previous) {
      if (previous.region !== current.region) value += "\n\n";
      else if (previous.line !== current.line) {
        const gap = current.bounds.top - previous.bounds.bottom;
        value += gap > Math.max(previous.bounds.height, current.bounds.height) * .8 ? "\n\n" : "\n";
      } else value += separator(previous, current);
    }
    value += slice;
    previous = current;
  }
  return value.trim();
}


function mergeRects(rects: DOMRect[]): DOMRect[] {
  const ordered = [...rects].sort((first, second) => (
    Math.abs(first.top - second.top) <= Math.min(first.height, second.height) * 0.45
      ? first.left - second.left
      : first.top - second.top
  ));
  const merged: DOMRect[] = [];
  for (const rect of ordered) {
    const previous = merged[merged.length - 1];
    if (previous !== undefined) {
      const minHeight = Math.max(1, Math.min(previous.height, rect.height));
      const verticalOverlap = overlap(previous.top, previous.bottom, rect.top, rect.bottom);
      const sameLine = verticalOverlap >= minHeight * 0.55;
      const gap = rect.left - previous.right;
      const maxWordGap = Math.max(24, minHeight * 1.8);
      if (sameLine && gap >= -minHeight * 0.6 && gap <= maxWordGap) {
        const left = Math.min(previous.left, rect.left);
        const top = Math.min(previous.top, rect.top);
        const right = Math.max(previous.right, rect.right);
        const bottom = Math.max(previous.bottom, rect.bottom);
        merged[merged.length - 1] = new DOMRect(left, top, right - left, bottom - top);
        continue;
      }
    }
    merged.push(rect);
  }
  return merged;
}


function nearestFragment(
  fragments: VisualFragment[],
  x: number,
  y: number,
): VisualFragment | null {
  return fragments.reduce<VisualFragment | null>((best, candidate) => {
    if (best === null) return candidate;
    return rectDistance(candidate.bounds, x, y) < rectDistance(best.bounds, x, y)
      ? candidate
      : best;
  }, null);
}


function proportionalOffset(fragment: VisualFragment, x: number): number {
  const ratio = fragment.bounds.width <= 0
    ? 0
    : (x - fragment.bounds.left) / fragment.bounds.width;
  return Math.max(0, Math.min(fragment.node.data.length, Math.round(ratio * fragment.node.data.length)));
}


function measuredOffset(fragment: VisualFragment, x: number, y: number): number {
  const length = fragment.node.data.length;
  if (length === 0 || length > 512) return proportionalOffset(fragment, x);
  let bestIndex = -1;
  let bestRect: DOMRect | null = null;
  let bestScore = Number.POSITIVE_INFINITY;
  for (let index = 0; index < length; index += 1) {
    for (const rect of fragment.measure(index, index + 1)) {
      if (rect.width <= 0 || rect.height <= 0) continue;
      const centerX = (rect.left + rect.right) / 2;
      const centerY = (rect.top + rect.bottom) / 2;
      const score = (
        rectDistance(rect, x, y) * 100
        + Math.abs(centerY - y) * 2
        + Math.abs(centerX - x)
      );
      if (score < bestScore) {
        bestIndex = index;
        bestRect = rect;
        bestScore = score;
      }
    }
  }
  if (bestIndex < 0 || bestRect === null) return proportionalOffset(fragment, x);
  return x >= (bestRect.left + bestRect.right) / 2 ? bestIndex + 1 : bestIndex;
}


function sameColumn(anchor: DOMRect, candidate: DOMRect): boolean {
  const maximumHeight = Math.max(anchor.height, candidate.height);
  const edgeTolerance = Math.max(48, maximumHeight * 3);
  if (Math.abs(anchor.left - candidate.left) <= edgeTolerance) {
    return true;
  }
  const horizontalOverlap = overlap(anchor.left, anchor.right, candidate.left, candidate.right);
  const minimumWidth = Math.max(1, Math.min(anchor.width, candidate.width));
  const centerDistance = Math.abs(
    (anchor.left + anchor.right) / 2 - (candidate.left + candidate.right) / 2,
  );
  return horizontalOverlap / minimumWidth >= 0.7 && centerDistance <= minimumWidth * 0.2;
}


export function resolveVisualSelection(
  index: VisualTextIndex,
  start: VisualTextHit,
  requestedEnd: VisualTextHit,
  options: ResolveOptions,
): SelectionCapture {
  if (options.scale <= 0 || !Number.isInteger(options.page) || options.page < 1) {
    return { selected: null, hint: SELECTION_HINT };
  }
  const startFragment = index.byNode.get(start.node);
  let endFragment = index.byNode.get(requestedEnd.node);
  if (startFragment === undefined || endFragment === undefined) {
    return { selected: null, hint: SELECTION_HINT };
  }
  let end = requestedEnd;
  const anchorBounds = index.lineBounds.get(startFragment.line) ?? startFragment.bounds;
  const anchored = index.fragments.filter((fragment) => {
    const bounds = index.lineBounds.get(fragment.line) ?? fragment.bounds;
    return sameColumn(anchorBounds, bounds);
  });
  if (!options.allowCrossRegion && !anchored.includes(endFragment)) {
    const replacement = nearestFragment(anchored, requestedEnd.clientX, requestedEnd.clientY);
    if (replacement === null) return { selected: null, hint: SELECTION_HINT };
    endFragment = replacement;
    const replacementOffset = requestedEnd.clientY > start.clientY + replacement.bounds.height * 0.25
      ? replacement.node.data.length
      : requestedEnd.clientY < start.clientY - replacement.bounds.height * 0.25
        ? 0
        : measuredOffset(replacement, requestedEnd.clientX, requestedEnd.clientY);
    end = {
      node: replacement.node,
      offset: replacementOffset,
      clientX: requestedEnd.clientX,
      clientY: requestedEnd.clientY,
    };
  }
  const ordered = options.allowCrossRegion
    ? index.fragments
    : anchored;
  const startIndex = ordered.indexOf(startFragment);
  const endIndex = ordered.indexOf(endFragment);
  if (startIndex < 0 || endIndex < 0) return { selected: null, hint: SELECTION_HINT };

  const forward = startIndex < endIndex || (startIndex === endIndex && start.offset <= end.offset);
  const firstIndex = Math.min(startIndex, endIndex);
  const lastIndex = Math.max(startIndex, endIndex);
  const chosen = ordered.slice(firstIndex, lastIndex + 1);
  const firstOffset = Math.max(0, Math.min(
    chosen[0]?.node.data.length ?? 0,
    forward ? start.offset : end.offset,
  ));
  const lastOffset = Math.max(0, Math.min(
    chosen[chosen.length - 1]?.node.data.length ?? 0,
    forward ? end.offset : start.offset,
  ));
  const slices: string[] = [];
  const measured: DOMRect[] = [];
  for (let position = 0; position < chosen.length; position += 1) {
    const fragment = chosen[position];
    if (fragment === undefined) continue;
    const from = position === 0 ? firstOffset : 0;
    const to = position === chosen.length - 1 ? lastOffset : fragment.node.data.length;
    if (to <= from) {
      slices.push("");
      continue;
    }
    slices.push(fragment.node.data.slice(from, to));
    measured.push(...fragment.measure(from, to).filter((rect) => rect.width > 0 && rect.height > 0));
  }
  const selectedText = joinSlices(chosen, slices);
  const rawText = clipboardSlices(chosen, slices);
  const clientRects = mergeRects(measured);
  if (selectedText === "" || clientRects.length === 0 || clientRects.length > MAX_VISIBLE_RECTS) {
    return { selected: null, hint: SELECTION_HINT };
  }

  const beforeFragments = ordered.slice(0, firstIndex + 1);
  const beforeSlices = beforeFragments.map((fragment, position) => (
    position === beforeFragments.length - 1
      ? fragment.node.data.slice(0, firstOffset)
      : fragment.node.data
  ));
  const afterFragments = ordered.slice(lastIndex);
  const afterSlices = afterFragments.map((fragment, position) => (
    position === 0 ? fragment.node.data.slice(lastOffset) : fragment.node.data
  ));
  const contextBefore = Array.from(joinSlices(beforeFragments, beforeSlices))
    .slice(-MAX_CONTEXT_CODE_POINTS)
    .join("");
  const contextAfter = Array.from(joinSlices(afterFragments, afterSlices))
    .slice(0, MAX_CONTEXT_CODE_POINTS)
    .join("");
  const rects = clientRects.map((rect) => ({
    x: (rect.left - options.pageBounds.left) / options.scale,
    y: (rect.top - options.pageBounds.top) / options.scale,
    width: rect.width / options.scale,
    height: rect.height / options.scale,
  }));
  const pdfQuads = clientRects.map((rect) => {
    const left = rect.left - options.pageBounds.left;
    const right = rect.right - options.pageBounds.left;
    const top = rect.top - options.pageBounds.top;
    const bottom = rect.bottom - options.pageBounds.top;
    return [
      viewportPointToPdfPoint({ x: left, y: top }, options.transform),
      viewportPointToPdfPoint({ x: right, y: top }, options.transform),
      viewportPointToPdfPoint({ x: left, y: bottom }, options.transform),
      viewportPointToPdfPoint({ x: right, y: bottom }, options.transform),
    ].flatMap((point) => [point.x, point.y]);
  });
  return {
    selected: {
      ...options.metadata,
      pdfPage: options.page,
      text: selectedText,
      rawText,
      copyText: cleanPdfCopyText(rawText, ordered.map(fragment => fragment.node.data).join("\n")),
      contextBefore,
      contextAfter,
      rects,
      pdfQuads,
      buttonAnchor: {
        x: (end.clientX - options.pageBounds.left) / options.scale,
        y: (end.clientY - options.pageBounds.top) / options.scale,
      },
      viewportAnchor: { x: end.clientX, y: end.clientY },
      translationEligible: Array.from(selectedText).length <= MAX_TRANSLATION_CODE_POINTS,
    },
    hint: null,
  };
}


function measureNodeRange(node: Text, start: number, end: number): DOMRect[] {
  const range = node.ownerDocument.createRange();
  range.setStart(node, start);
  range.setEnd(node, end);
  if (typeof range.getClientRects !== "function") return [];
  return Array.from(range.getClientRects());
}


function domSeeds(layer: HTMLElement): VisualFragmentSeed[] {
  const walker = layer.ownerDocument.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
  const seeds: VisualFragmentSeed[] = [];
  for (let current = walker.nextNode(); current !== null; current = walker.nextNode()) {
    if (!(current instanceof Text) || normalizeWhitespace(current.data) === "") continue;
    const rects = measureNodeRange(current, 0, current.data.length)
      .filter((rect) => rect.width > 0 && rect.height > 0);
    if (rects.length === 0) continue;
    const left = Math.min(...rects.map((rect) => rect.left));
    const top = Math.min(...rects.map((rect) => rect.top));
    const right = Math.max(...rects.map((rect) => rect.right));
    const bottom = Math.max(...rects.map((rect) => rect.bottom));
    seeds.push({
      node: current,
      bounds: new DOMRect(left, top, right - left, bottom - top),
      measure: (start, end) => measureNodeRange(current, start, end),
    });
  }
  return seeds;
}


function pageTransform(page: HTMLElement): [number, number, number, number, number, number] | null {
  try {
    const value = JSON.parse(page.dataset.pdfTransform ?? "null") as unknown;
    if (!Array.isArray(value) || value.length !== 6 || !value.every(Number.isFinite)) return null;
    return value as [number, number, number, number, number, number];
  } catch {
    return null;
  }
}


type CaretDocument = Document & {
  caretPositionFromPoint?(x: number, y: number): { offsetNode: Node; offset: number } | null;
  caretRangeFromPoint?(x: number, y: number): Range | null;
};


function trustedCaretHit(
  index: VisualTextIndex,
  node: Node | undefined,
  offset: number | undefined,
  x: number,
  y: number,
): VisualTextHit | null {
  if (!(node instanceof Text) || offset === undefined) return null;
  const fragment = index.byNode.get(node);
  if (fragment === undefined || node.data.length === 0) return null;
  const clamped = Math.max(0, Math.min(node.data.length, offset));
  const from = Math.max(0, clamped - 1);
  const to = Math.min(node.data.length, Math.max(clamped + 1, from + 1));
  const distance = Math.min(
    ...fragment.measure(from, to)
      .filter((rect) => rect.width > 0 && rect.height > 0)
      .map((rect) => rectDistance(rect, x, y)),
  );
  const tolerance = Math.max(6, fragment.bounds.height * 0.5);
  if (!Number.isFinite(distance) || distance > tolerance) return null;
  return { node, offset: clamped, clientX: x, clientY: y };
}


function hitsAtPoint(
  index: VisualTextIndex,
  ownerDocument: Document,
  x: number,
  y: number,
  strict: boolean,
): VisualTextHit[] {
  const caretDocument = ownerDocument as CaretDocument;
  const position = caretDocument.caretPositionFromPoint?.(x, y);
  const range = position === undefined || position === null
    ? caretDocument.caretRangeFromPoint?.(x, y) ?? null
    : null;
  const node = position?.offsetNode ?? range?.startContainer;
  const offset = position?.offset ?? range?.startOffset;
  const caretHit = trustedCaretHit(index, node, offset, x, y);
  if (caretHit !== null) return [caretHit];
  const hits: VisualTextHit[] = [];
  const ordered = [...index.fragments].sort((first, second) => (
    rectDistance(first.bounds, x, y) - rectDistance(second.bounds, x, y)
  ));
  for (const fragment of ordered) {
    const distance = rectDistance(fragment.bounds, x, y);
    const maximumDistance = strict ? Math.max(6, fragment.bounds.height * 0.5) : 48;
    if (distance > maximumDistance) continue;
    if (hits.some((hit) => hit.node === fragment.node)) continue;
    hits.push({
      node: fragment.node,
      offset: measuredOffset(fragment, x, y),
      clientX: x,
      clientY: y,
    });
  }
  if (!strict && hits.length === 0 && ordered.length > 0) {
    const byLine = [...index.fragments].sort((first, second) => {
      const dy1 = Math.max(first.bounds.top - y, 0, y - first.bounds.bottom);
      const dy2 = Math.max(second.bounds.top - y, 0, y - second.bounds.bottom);
      const dx1 = Math.max(first.bounds.left - x, 0, x - first.bounds.right);
      const dx2 = Math.max(second.bounds.left - x, 0, x - second.bounds.right);
      return (dy1 * 8 + dx1) - (dy2 * 8 + dx2);
    });
    const nearest = byLine[0]!;
    hits.push({
      node: nearest.node,
      offset: measuredOffset(nearest, x, y),
      clientX: x,
      clientY: y,
    });
  }
  return hits;
}


function hitPairScore(
  index: VisualTextIndex,
  start: VisualTextHit,
  end: VisualTextHit,
): number {
  const startFragment = index.byNode.get(start.node);
  const endFragment = index.byNode.get(end.node);
  if (startFragment === undefined || endFragment === undefined) return Number.POSITIVE_INFINITY;
  const startBounds = index.lineBounds.get(startFragment.line) ?? startFragment.bounds;
  const endBounds = index.lineBounds.get(endFragment.line) ?? endFragment.bounds;
  const alignment = Math.min(
    Math.abs(startBounds.left - endBounds.left),
    Math.abs(startBounds.right - endBounds.right),
  );
  const pointerDistance = (
    rectDistance(startFragment.bounds, start.clientX, start.clientY)
    + rectDistance(endFragment.bounds, end.clientX, end.clientY)
  );
  const lineCenterDistance = (
    Math.abs((startFragment.bounds.top + startFragment.bounds.bottom) / 2 - start.clientY)
    + Math.abs((endFragment.bounds.top + endFragment.bounds.bottom) / 2 - end.clientY)
  );
  return (
    (sameColumn(startBounds, endBounds) ? 0 : 100_000)
    + alignment
    + pointerDistance * 8
    + lineCenterDistance * 4
  );
}


function bestHitPair(
  index: VisualTextIndex,
  starts: VisualTextHit[],
  ends: VisualTextHit[],
): [VisualTextHit, VisualTextHit] | null {
  let best: [VisualTextHit, VisualTextHit] | null = null;
  let bestScore = Number.POSITIVE_INFINITY;
  for (const start of starts) {
    for (const end of ends) {
      const score = hitPairScore(index, start, end);
      if (score < bestScore) {
        best = [start, end];
        bestScore = score;
      }
    }
  }
  return best;
}


export function beginVisualSelection(
  target: EventTarget | null,
  clientX: number,
  clientY: number,
  metadata: SelectionMetadata,
  scale: number,
): VisualSelectionGesture | null {
  if (!(target instanceof Element) || scale <= 0) return null;
  const page = target.closest<HTMLElement>("[data-page-number]");
  const pageNumber = Number(page?.dataset.pageNumber);
  const transform = page === null ? null : pageTransform(page);
  if (page === null || !Number.isInteger(pageNumber) || pageNumber < 1 || transform === null) {
    return null;
  }
  const directLayer = target.closest<HTMLElement>(".textLayer");
  const layers = directLayer === null
    ? Array.from(page.querySelectorAll<HTMLElement>(":scope > .textLayer"))
        .sort((first, second) => Number(second.classList.contains("ocr-text-layer"))
          - Number(first.classList.contains("ocr-text-layer")))
    : [directLayer];
  for (const layer of layers) {
    const index = createVisualTextIndex(domSeeds(layer));
    const startCandidates = hitsAtPoint(index, layer.ownerDocument, clientX, clientY, true);
    const start = startCandidates[0] ?? null;
    if (start === null) continue;
    return {
      index,
      start,
      startCandidates,
      ownerDocument: layer.ownerDocument,
      options: {
        metadata,
        page: pageNumber,
        pageBounds: page.getBoundingClientRect(),
        transform,
        scale,
      },
    };
  }
  return null;
}


export function captureVisualSelection(
  gesture: VisualSelectionGesture,
  clientX: number,
  clientY: number,
  allowCrossRegion: boolean,
): SelectionCapture {
  const endCandidates = hitsAtPoint(
    gesture.index,
    gesture.ownerDocument,
    clientX,
    clientY,
    false,
  );
  const pair = allowCrossRegion
    ? endCandidates[0] === undefined ? null : [gesture.start, endCandidates[0]] as const
    : bestHitPair(gesture.index, gesture.startCandidates, endCandidates);
  if (pair === null) return { selected: null, hint: SELECTION_HINT };
  return resolveVisualSelection(gesture.index, pair[0], pair[1], {
    ...gesture.options,
    allowCrossRegion,
  });
}
