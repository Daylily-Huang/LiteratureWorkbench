import { afterEach, describe, expect, it, vi } from "vitest";

import { createAnnotation, mergeDocumentIdentity } from "./annotations";
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
});
