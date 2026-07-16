import { afterEach, expect, test, vi } from "vitest";

import { DocumentApiError, browserDocumentApi } from "./documents";

afterEach(() => vi.unstubAllGlobals());

function item(index: number, status = "valid_pdf") {
  return {
    document_id: `document-${index}`,
    current_document_version_id: `version-${index}`,
    content_id: `${index}`.padStart(64, "a"),
    filename: `paper-${index}.pdf`,
    relative_folder: index === 200 ? "生态学/相机陷阱" : "",
    status,
    cloud_policy: {
      auto_cloud_eligible: true,
      authorization_source: index === 200 ? "registered_library" : "confirmed_import",
    },
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
  expect(documents.at(-1)).toEqual({
    document_id: "document-200",
    filename: "paper-200.pdf",
    relative_folder: "生态学/相机陷阱",
    authorization_source: "registered_library",
  });
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
