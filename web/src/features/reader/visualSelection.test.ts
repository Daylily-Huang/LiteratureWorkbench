import { describe, expect, it } from "vitest";

import {
  captureVisualSelection,
  createVisualTextIndex,
  resolveVisualSelection,
  type VisualFragmentSeed,
  type VisualSelectionGesture,
} from "./visualSelection";


function rect(left: number, top: number, width: number, height = 20): DOMRect {
  return {
    left,
    top,
    right: left + width,
    bottom: top + height,
    width,
    height,
    x: left,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}


function seed(text: string, bounds: DOMRect, characterWidth = 10): VisualFragmentSeed {
  const node = document.createTextNode(text);
  return {
    node,
    bounds,
    measure(start, end) {
      return [rect(
        bounds.left + start * characterWidth,
        bounds.top,
        Math.max(0, end - start) * characterWidth,
        bounds.height,
      )];
    },
  };
}


const metadata = {
  documentId: "document-a",
  documentVersionId: "version-a",
  contentId: "content-a",
};


describe("visual PDF text selection", () => {
  it("uses visual column order when DOM order interleaves a sidebar and footer", () => {
    const sidebar = seed("Published: October 25, 2021", rect(190, 100, 180));
    const first = seed("Microsatellites are neutral,", rect(330, 100, 290));
    const footer = seed("PLOS ONE October 25, 2021", rect(20, 500, 280));
    const second = seed("codominant markers.", rect(330, 124, 200));
    const index = createVisualTextIndex([sidebar, first, footer, second]);

    const captured = resolveVisualSelection(index, {
      node: first.node,
      offset: 0,
      clientX: 330,
      clientY: 110,
    }, {
      node: second.node,
      offset: second.node.data.length,
      clientX: 530,
      clientY: 134,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 700, 600),
      transform: [1, 0, 0, -1, 0, 600],
      scale: 1,
    });

    expect(captured.hint).toBeNull();
    expect(captured.selected?.text).toBe("Microsatellites are neutral, codominant markers.");
    expect(captured.selected?.text).not.toContain("Published");
    expect(captured.selected?.text).not.toContain("PLOS ONE");
    expect(captured.selected?.rects).toHaveLength(2);
  });

  it("keeps overlapping PDF.js text bounds in their DOM-backed visual columns", () => {
    const leftOne = seed("left first", rect(100, 100, 530));
    const leftTwo = seed("left second", rect(100, 124, 540));
    const rightOne = seed("right first", rect(360, 100, 540));
    const rightTwo = seed("right second", rect(360, 124, 550));
    const index = createVisualTextIndex([leftOne, leftTwo, rightOne, rightTwo]);

    const captured = resolveVisualSelection(index, {
      node: leftOne.node,
      offset: 0,
      clientX: 100,
      clientY: 110,
    }, {
      node: leftTwo.node,
      offset: leftTwo.node.data.length,
      clientX: 640,
      clientY: 134,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 1000, 600),
      transform: [1, 0, 0, -1, 0, 600],
      scale: 1,
    });

    expect(captured.selected?.text).toBe("left first left second");
    expect(captured.selected?.text).not.toContain("right");
  });

  it("does not admit a foreign-column styled fragment by a coincidental right edge", () => {
    const leftOne = seed("left first", rect(100, 100, 530));
    const leftTwo = seed("left middle", rect(100, 124, 280));
    const leftSuffix = seed(" left suffix", rect(230, 124, 290));
    const leftThree = seed("left last", rect(100, 148, 520));
    const rightOne = seed("right first", rect(360, 100, 540));
    const rightTwo = seed("right middle", rect(360, 124, 395));
    const rightSuffix = seed(" right suffix", rect(500, 124, 130));
    const rightThree = seed("right last", rect(360, 148, 530));
    const index = createVisualTextIndex([
      leftOne, leftTwo, leftSuffix, leftThree,
      rightOne, rightTwo, rightSuffix, rightThree,
    ]);

    const captured = resolveVisualSelection(index, {
      node: leftOne.node,
      offset: 0,
      clientX: 100,
      clientY: 110,
    }, {
      node: leftThree.node,
      offset: leftThree.node.data.length,
      clientX: 620,
      clientY: 158,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 1000, 600),
      transform: [1, 0, 0, -1, 0, 600],
      scale: 1,
    });

    expect(captured.selected?.text).toBe("left first left middle left suffix left last");
    expect(captured.selected?.text).not.toContain("right");
  });

  it("does not insert a space before punctuation split from an italic PDF span", () => {
    const italicName = seed("Tragulus versicolor", rect(100, 100, 170));
    const comma = seed(",", rect(274, 100, 5));
    const suffix = seed(" a", rect(280, 100, 15));
    const index = createVisualTextIndex([italicName, comma, suffix]);

    const captured = resolveVisualSelection(index, {
      node: italicName.node,
      offset: 0,
      clientX: 100,
      clientY: 110,
    }, {
      node: suffix.node,
      offset: suffix.node.data.length,
      clientX: 295,
      clientY: 110,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 700, 600),
      transform: [1, 0, 0, -1, 0, 600],
      scale: 1,
    });

    expect(captured.selected?.text).toBe("Tragulus versicolor, a");
  });

  it("preserves a word boundary before an adjacent italic scientific name", () => {
    const prefix = seed("The silver-backed chevrotain", rect(100, 100, 210));
    const italicName = seed("Tragulus versicolor", rect(309, 100, 170));
    const comma = seed(",", rect(483, 100, 5));
    const suffix = seed(" a", rect(489, 100, 15));
    const index = createVisualTextIndex([prefix, italicName, comma, suffix]);

    const captured = resolveVisualSelection(index, {
      node: prefix.node,
      offset: 0,
      clientX: 100,
      clientY: 110,
    }, {
      node: suffix.node,
      offset: suffix.node.data.length,
      clientX: 504,
      clientY: 110,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 700, 600),
      transform: [1, 0, 0, -1, 0, 600],
      scale: 1,
    });

    expect(captured.selected?.text).toBe(
      "The silver-backed chevrotain Tragulus versicolor, a",
    );
  });

  it("restores PDF.js whitespace spans hidden from the visual index", () => {
    const intense = seed("intense,", rect(100, 100, 90));
    const widespread = seed("widespread", rect(150, 100, 110));
    const conjunction = seed("and", rect(220, 100, 35));
    const rapidly = seed("rapidly", rect(245, 100, 70));
    const layer = document.createElement("div");
    for (const fragment of [intense, widespread, conjunction, rapidly]) {
      if (layer.childNodes.length > 0) {
        const whitespace = document.createElement("span");
        whitespace.textContent = " ";
        layer.append(whitespace);
      }
      const span = document.createElement("span");
      span.append(fragment.node);
      layer.append(span);
    }
    const index = createVisualTextIndex([intense, widespread, conjunction, rapidly]);

    const captured = resolveVisualSelection(index, {
      node: intense.node,
      offset: 0,
      clientX: 100,
      clientY: 110,
    }, {
      node: rapidly.node,
      offset: rapidly.node.data.length,
      clientX: 315,
      clientY: 110,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 700, 600),
      transform: [1, 0, 0, -1, 0, 600],
      scale: 1,
    });

    expect(captured.selected?.text).toBe("intense, widespread and rapidly");
  });

  it("keeps overlapping styled spans inside a full-width PDF line selection", () => {
    const prefix = seed(
      "human pressures. Moreover, pressures are perversely",
      rect(559, 100, 382),
    );
    const intense = seed("intense,", rect(790, 100, 52));
    const widespread = seed("widespread", rect(827, 100, 76));
    const conjunction = seed("and", rect(877, 100, 25));
    const rapidly = seed("rapidly", rect(897, 100, 45));
    const suffix = seed("intensifying in places with high biodiversity.", rect(559, 124, 617));
    const fragments = [prefix, intense, widespread, conjunction, rapidly, suffix];
    const layer = document.createElement("div");
    for (const fragment of fragments) {
      if (layer.childNodes.length > 0) {
        const whitespace = document.createElement("span");
        whitespace.textContent = " ";
        layer.append(whitespace);
      }
      const span = document.createElement("span");
      span.append(fragment.node);
      layer.append(span);
    }
    const index = createVisualTextIndex(fragments);

    const captured = resolveVisualSelection(index, {
      node: prefix.node,
      offset: 0,
      clientX: 559,
      clientY: 110,
    }, {
      node: suffix.node,
      offset: suffix.node.data.length,
      clientX: 1176,
      clientY: 134,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 1300, 800),
      transform: [1, 0, 0, -1, 0, 800],
      scale: 1,
    });

    expect(captured.selected?.text).toBe(
      "human pressures. Moreover, pressures are perversely intense, widespread and rapidly intensifying in places with high biodiversity.",
    );
  });

  it("keeps an out-of-order right suffix on its visual line", () => {
    const aims = seed("Aims", rect(20, 100, 50));
    const opening = seed(
      "Subalpine dark coniferous forests are an important representative of the cold-temperate coniferous forests",
      rect(90, 100, 1000),
    );
    const second = seed(
      "in China. Dark coniferous forests dominated by spruce and fir are the main body of",
      rect(20, 124, 1070),
    );
    const third = seed(
      "subalpine forests on the eastern edge of the Qingzang Plateau and an important ecological defense in the upper",
      rect(20, 148, 1070),
    );
    const prefix = seed(
      "such a dark coniferous forest based on the first census data of the 25.2 hm²",
      rect(20, 172, 760),
    );
    const nextLine = seed("western Sichuan.", rect(20, 196, 160));
    const outOfOrderSuffix = seed(
      " Wanglang forest dynamics plot in",
      rect(782, 172, 308),
    );
    const index = createVisualTextIndex([
      aims,
      opening,
      second,
      third,
      prefix,
      nextLine,
      outOfOrderSuffix,
    ]);

    const captured = resolveVisualSelection(index, {
      node: aims.node,
      offset: 0,
      clientX: 20,
      clientY: 110,
    }, {
      node: nextLine.node,
      offset: nextLine.node.data.length,
      clientX: 180,
      clientY: 206,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 1200, 800),
      transform: [1, 0, 0, -1, 0, 800],
      scale: 1,
    });

    expect(captured.selected?.text).toContain(
      "data of the 25.2 hm² Wanglang forest dynamics plot in western Sichuan.",
    );
    expect(captured.selected?.rects).toHaveLength(6);
  });

  it("isolates the overlapping two-column geometry observed in the PNAS fixture", () => {
    const left = [
      seed("The population extinction pulse we describe here shows, from a", rect(334.69, 253.73, 531.96, 16)),
      seed("quantitative viewpoint, that Earth", rect(334.69, 263.67, 280.96, 16)),
      seed("’", rect(461.45, 263.67, 16, 16)),
      seed("s sixth mass extinction is more", rect(463.5, 263.67, 250.23, 16)),
      seed("severe than perceived when looking exclusively at species extinc-", rect(334.69, 273.7, 526.5, 16)),
      seed("tions. Therefore, humanity needs to address anthropogenic popula-", rect(334.69, 283.64, 534.49, 16)),
    ];
    const right = [
      seed("especially because many of those species were obscure and had", rect(593.25, 252.95, 525.32, 16)),
      seed("limited ranges, such as the Catarina pupfish (", rect(593.25, 263.13, 394.62, 16)),
      seed("Megupsilon aporus", rect(766.63, 263.13, 136.83, 16)),
      seed(",", rect(836.06, 263.13, 8, 16)),
      seed("extinct in 2014), a tiny fish from Mexico, or the Christmas Island", rect(593.25, 273.31, 552.59, 16)),
      seed("pipistrelle (", rect(593.25, 283.56, 112.78, 16)),
      seed("Pipistrellus murrayi", rect(637.7, 283.56, 160.43, 16)),
      seed(", extinct in 2009), a bat that van-", rect(708.89, 283.56, 310.49, 16)),
    ];
    const index = createVisualTextIndex([...left, ...right]);

    const captured = resolveVisualSelection(index, {
      node: left[0]!.node,
      offset: 0,
      clientX: 334.89,
      clientY: 261.23,
    }, {
      node: left[5]!.node,
      offset: 6,
      clientX: 377.33,
      clientY: 291.14,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 1200, 900),
      transform: [1, 0, 0, -1, 0, 900],
      scale: 1,
    });

    expect(captured.selected?.text).toBe(
      "The population extinction pulse we describe here shows, from a quantitative viewpoint, that Earth’s sixth mass extinction is more severe than perceived when looking exclusively at species extinc- tions.",
    );
    expect(captured.selected?.text).not.toContain("especially because");
  });

  it("prefers pointer-near hits when the browser caret reports a distant column", () => {
    const leftOne = seed("left first", rect(100, 100, 100));
    const leftTwo = seed("left second", rect(100, 124, 110));
    const rightOne = seed("right first", rect(500, 100, 100));
    const rightTwo = seed("right second", rect(500, 124, 110));
    const index = createVisualTextIndex([leftOne, leftTwo, rightOne, rightTwo]);
    const gesture: VisualSelectionGesture = {
      index,
      start: {
        node: rightOne.node, offset: 0, clientX: 100, clientY: 110,
      },
      startCandidates: [{
        node: rightOne.node, offset: 0, clientX: 100, clientY: 110,
      }, {
        node: leftOne.node, offset: 0, clientX: 100, clientY: 110,
      }],
      ownerDocument: document,
      options: {
        metadata,
        page: 1,
        pageBounds: rect(0, 0, 800, 600),
        transform: [1, 0, 0, -1, 0, 600],
        scale: 1,
      },
    };
    const descriptor = Object.getOwnPropertyDescriptor(document, "caretPositionFromPoint");
    Object.defineProperty(document, "caretPositionFromPoint", {
      configurable: true,
      value: () => ({ offsetNode: rightTwo.node, offset: rightTwo.node.data.length }),
    });
    try {
      const captured = captureVisualSelection(gesture, 210, 134, false);
      expect(captured.selected?.text).toBe("left first left second");
    } finally {
      if (descriptor === undefined) Reflect.deleteProperty(document, "caretPositionFromPoint");
      else Object.defineProperty(document, "caretPositionFromPoint", descriptor);
    }
  });

  it("prefers the nearest line center when adjacent text bounds overlap vertically", () => {
    const first = seed("first line", rect(100, 100, 100, 20));
    const second = seed("second line", rect(100, 110, 110, 20));
    const index = createVisualTextIndex([first, second]);
    const gesture: VisualSelectionGesture = {
      index,
      start: { node: first.node, offset: 0, clientX: 100, clientY: 110 },
      startCandidates: [{ node: first.node, offset: 0, clientX: 100, clientY: 110 }],
      ownerDocument: document,
      options: {
        metadata,
        page: 1,
        pageBounds: rect(0, 0, 800, 600),
        transform: [1, 0, 0, -1, 0, 600],
        scale: 1,
      },
    };
    const descriptor = Object.getOwnPropertyDescriptor(document, "caretPositionFromPoint");
    Object.defineProperty(document, "caretPositionFromPoint", {
      configurable: true,
      value: () => ({ offsetNode: second.node, offset: second.node.data.length }),
    });
    try {
      const captured = captureVisualSelection(gesture, 150, 110, false);
      expect(captured.selected?.text).toBe("first");
    } finally {
      if (descriptor === undefined) Reflect.deleteProperty(document, "caretPositionFromPoint");
      else Object.defineProperty(document, "caretPositionFromPoint", descriptor);
    }
  });

  it("keeps a browser caret endpoint when its character box is under the pointer", () => {
    const first = seed("start", rect(100, 100, 50, 20));
    const desired = seed("desired", rect(140, 120, 70, 10));
    const alignedButWrong = seed("wrong", rect(120, 131, 80, 10));
    const index = createVisualTextIndex([first, desired, alignedButWrong]);
    const gesture: VisualSelectionGesture = {
      index,
      start: { node: first.node, offset: 0, clientX: 100, clientY: 110 },
      startCandidates: [{ node: first.node, offset: 0, clientX: 100, clientY: 110 }],
      ownerDocument: document,
      options: {
        metadata,
        page: 1,
        pageBounds: rect(0, 0, 800, 600),
        transform: [1, 0, 0, -1, 0, 600],
        scale: 1,
      },
    };
    const descriptor = Object.getOwnPropertyDescriptor(document, "caretPositionFromPoint");
    Object.defineProperty(document, "caretPositionFromPoint", {
      configurable: true,
      value: () => ({ offsetNode: desired.node, offset: 5 }),
    });
    try {
      const captured = captureVisualSelection(gesture, 190, 130, false);
      expect(captured.selected?.text).toBe("start desir");
      expect(captured.selected?.text).not.toContain("wrong");
    } finally {
      if (descriptor === undefined) Reflect.deleteProperty(document, "caretPositionFromPoint");
      else Object.defineProperty(document, "caretPositionFromPoint", descriptor);
    }
  });

  it("inserts a separator between distinct visual lines with overlapping bounds", () => {
    const first = seed("first line", rect(100, 100, 100, 20));
    const second = seed("second line", rect(100, 110, 110, 20));
    const index = createVisualTextIndex([first, second]);

    const captured = resolveVisualSelection(index, {
      node: first.node, offset: 0, clientX: 100, clientY: 110,
    }, {
      node: second.node, offset: second.node.data.length, clientX: 210, clientY: 120,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 800, 600),
      transform: [1, 0, 0, -1, 0, 600],
      scale: 1,
    });

    expect(captured.selected?.text).toBe("first line second line");
  });

  it("uses measured character boxes when a PDF span width is distorted", () => {
    const fragment = seed("tions. more", rect(100, 100, 500, 20), 10);
    const index = createVisualTextIndex([fragment]);
    const gesture: VisualSelectionGesture = {
      index,
      start: { node: fragment.node, offset: 0, clientX: 100, clientY: 110 },
      startCandidates: [{ node: fragment.node, offset: 0, clientX: 100, clientY: 110 }],
      ownerDocument: document,
      options: {
        metadata,
        page: 1,
        pageBounds: rect(0, 0, 800, 600),
        transform: [1, 0, 0, -1, 0, 600],
        scale: 1,
      },
    };
    const descriptor = Object.getOwnPropertyDescriptor(document, "caretPositionFromPoint");
    Object.defineProperty(document, "caretPositionFromPoint", {
      configurable: true,
      value: () => null,
    });
    try {
      const captured = captureVisualSelection(gesture, 159.8, 110, false);
      expect(captured.selected?.text).toBe("tions.");
    } finally {
      if (descriptor === undefined) Reflect.deleteProperty(document, "caretPositionFromPoint");
      else Object.defineProperty(document, "caretPositionFromPoint", descriptor);
    }
  });

  it("keeps exact character offsets and derives matching PDF quads", () => {
    const fragment = seed("precisely", rect(100, 50, 90));
    const index = createVisualTextIndex([fragment]);

    const captured = resolveVisualSelection(index, {
      node: fragment.node,
      offset: 1,
      clientX: 110,
      clientY: 60,
    }, {
      node: fragment.node,
      offset: 7,
      clientX: 170,
      clientY: 60,
    }, {
      metadata,
      page: 2,
      pageBounds: rect(10, 20, 600, 800),
      transform: [2, 0, 0, -2, 0, 800],
      scale: 2,
    });

    expect(captured.selected).toMatchObject({
      pdfPage: 2,
      text: "recise",
      rects: [{ x: 50, y: 15, width: 30, height: 10 }],
      pdfQuads: [[50, 385, 80, 385, 50, 375, 80, 375]],
    });
  });

  it("produces identical text and geometry for reverse dragging", () => {
    const first = seed("first line", rect(300, 100, 100));
    const second = seed("second line", rect(300, 124, 110));
    const index = createVisualTextIndex([first, second]);
    const options = {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 700, 600),
      transform: [1, 0, 0, -1, 0, 600] as [number, number, number, number, number, number],
      scale: 1,
    };
    const forward = resolveVisualSelection(index, {
      node: first.node, offset: 0, clientX: 300, clientY: 110,
    }, {
      node: second.node, offset: second.node.data.length, clientX: 410, clientY: 134,
    }, options);
    const reverse = resolveVisualSelection(index, {
      node: second.node, offset: second.node.data.length, clientX: 410, clientY: 134,
    }, {
      node: first.node, offset: 0, clientX: 300, clientY: 110,
    }, options);

    expect(reverse.selected?.text).toBe(forward.selected?.text);
    expect(reverse.selected?.rects).toEqual(forward.selected?.rects);
    expect(reverse.selected?.pdfQuads).toEqual(forward.selected?.pdfQuads);
    expect(reverse.selected?.viewportAnchor).toEqual({ x: 300, y: 110 });
  });

  it("locks a default drag to the starting visual region", () => {
    const leftOne = seed("left one", rect(20, 100, 100));
    const rightOne = seed("right one", rect(330, 100, 100));
    const leftTwo = seed("left two", rect(20, 124, 100));
    const rightTwo = seed("right two", rect(330, 124, 100));
    const index = createVisualTextIndex([leftOne, rightOne, leftTwo, rightTwo]);

    const captured = resolveVisualSelection(index, {
      node: rightOne.node,
      offset: 0,
      clientX: 330,
      clientY: 110,
    }, {
      node: leftTwo.node,
      offset: leftTwo.node.data.length,
      clientX: 120,
      clientY: 134,
    }, {
      metadata,
      page: 1,
      pageBounds: rect(0, 0, 700, 600),
      transform: [1, 0, 0, -1, 0, 600],
      scale: 1,
    });

    expect(captured.selected?.text).toBe("right one right two");
    expect(captured.selected?.text).not.toContain("left");
  });
});
