import { afterEach, expect, test, vi } from "vitest";

import { DocumentApiError, browserDocumentApi } from "./documents";

afterEach(() => vi.unstubAllGlobals());

function item(index: number, status = "valid_pdf") {
  return {
    document_id: `document-${index}`,
    current_document_version_id: `version-${index}`,
    content_id: `${index}`.padStart(64, "a"),
    filename: `paper-${index}.pdf`,
    status,
  };
}

test("loads every documents page and keeps only readable PDFs", async () => {
  const firstPage = Array.from({ length: 200 }, (_, index) => item(index));
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({
      items: firstPage,
      total: 201,
      page: 1,
      page_size: 200,
    }), { status: 200, headers: { "Content-Type": "application/json" } }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      items: [item(200), item(201, "unsupported")],
      total: 201,
      page: 2,
      page_size: 200,
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetcher);

  const documents = await browserDocumentApi.listDocuments();

  expect(documents).toHaveLength(201);
  expect(documents.at(-1)?.document_id).toBe("document-200");
  expect(fetcher).toHaveBeenNthCalledWith(
    1,
    "/api/documents?page=1&page_size=200",
    { credentials: "same-origin" },
  );
  expect(fetcher).toHaveBeenNthCalledWith(
    2,
    "/api/documents?page=2&page_size=200",
    { credentials: "same-origin" },
  );
});

test("maps HTTP and malformed payloads to a stable local error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("private detail", { status: 500 })));
  await expect(browserDocumentApi.listDocuments()).rejects.toEqual(
    new DocumentApiError("document_list_unavailable"),
  );

  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [{}], total: 1 }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })));
  await expect(browserDocumentApi.listDocuments()).rejects.toEqual(
    new DocumentApiError("document_list_unavailable"),
  );
});
