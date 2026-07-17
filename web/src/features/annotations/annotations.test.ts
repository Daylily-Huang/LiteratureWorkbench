import { afterEach, describe, expect, it, vi } from "vitest";

import {
  annotationColorName,
  createAnnotation,
  mergeDocumentIdentity,
  mergeSelectionIdentity,
} from "./annotations";
import type { SelectedText } from "../reader/selection";


describe("annotation API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends PDF-coordinate quads with the current document identity", async () => {
    const selected: SelectedText = {
      documentId: "document-a",
      documentVersionId: "version-a",
      contentId: "a".repeat(64),
      pdfPage: 3,
      text: "selected",
      contextBefore: "",
      contextAfter: "",
      rects: [],
      pdfQuads: [[10, 20, 30, 20, 10, 40, 30, 40]],
      buttonAnchor: { x: 0, y: 0 },
      viewportAnchor: { x: 0, y: 0 },
      translationEligible: true,
    };
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf-a" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        document_id: "document-a",
        current_document_version_id: "version-b",
        content_id: "b".repeat(64),
      }), { status: 201 }));
    vi.stubGlobal("fetch", fetcher);

    const result = await createAnnotation(selected, "highlight", "green");

    expect(result.current_document_version_id).toBe("version-b");
    expect(fetcher).toHaveBeenLastCalledWith(
      "/api/documents/document-a/annotations",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-LW-CSRF": "csrf-a" }),
        body: JSON.stringify({
          document_version_id: "version-a",
          content_id: "a".repeat(64),
          page: 3,
          type: "highlight",
          quads: selected.pdfQuads,
          color: "green",
          comment: "",
        }),
      }),
    );
  });

  it("merges a write response without losing the visible filename", () => {
    expect(mergeDocumentIdentity(
      {
        documentId: "document-a",
        documentVersionId: "version-a",
        contentId: "a".repeat(64),
        filename: "paper.pdf",
      },
      {
        document_id: "document-a",
        current_document_version_id: "version-b",
        content_id: "b".repeat(64),
      },
    )).toEqual({
      documentId: "document-a",
      documentVersionId: "version-b",
      contentId: "b".repeat(64),
      filename: "paper.pdf",
    });
  });

  it("moves a same-document selection to the annotation write identity", () => {
    const selected: SelectedText = {
      documentId: "document-a",
      documentVersionId: "version-a",
      contentId: "a".repeat(64),
      pdfPage: 3,
      text: "selected",
      contextBefore: "before",
      contextAfter: "after",
      rects: [{ x: 1, y: 2, width: 3, height: 4 }],
      pdfQuads: [[10, 20, 30, 20, 10, 40, 30, 40]],
      buttonAnchor: { x: 5, y: 6 },
      viewportAnchor: { x: 7, y: 8 },
      translationEligible: true,
    };

    expect(mergeSelectionIdentity(selected, {
      document_id: "document-a",
      current_document_version_id: "version-b",
      content_id: "b".repeat(64),
    })).toEqual({
      ...selected,
      documentVersionId: "version-b",
      contentId: "b".repeat(64),
    });
  });

  it("maps PDF RGB annotation colors back to the UI color names", () => {
    expect(annotationColorName([1, 0.843, 0])).toBe("yellow");
    expect(annotationColorName([0.35, 0.78, 0.42])).toBe("green");
    expect(annotationColorName([0.35, 0.68, 0.95])).toBe("blue");
    expect(annotationColorName([0.96, 0.47, 0.69])).toBe("pink");
    expect(annotationColorName([0.9, 0.16, 0.16])).toBe("red");
    expect(annotationColorName([0.08, 0.08, 0.08])).toBe("black");
    expect(annotationColorName([0.1, 0.2, 0.3])).toBeUndefined();
  });
});
