export type DocumentListItem = {
  document_id: string;
  filename: string;
  relative_folder: string;
  authorization_source: string;
};

export type DocumentApi = {
  listDocuments(): Promise<DocumentListItem[]>;
};

type DocumentPayload = DocumentListItem & {
  current_document_version_id: string | null;
  content_id: string | null;
  status: string;
  cloud_policy: {
    auto_cloud_eligible: boolean;
    authorization_source: string;
  };
};

type PagePayload = {
  items: DocumentPayload[];
  total: number;
};

export class DocumentApiError extends Error {
  constructor(readonly code: "document_list_unavailable") {
    super(code);
  }
}

function isDocument(value: unknown): value is DocumentPayload {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.document_id === "string" &&
    typeof item.filename === "string" &&
    typeof item.relative_folder === "string" &&
    (typeof item.current_document_version_id === "string" || item.current_document_version_id === null) &&
    (typeof item.content_id === "string" || item.content_id === null) &&
    typeof item.status === "string" &&
    typeof item.cloud_policy === "object" &&
    item.cloud_policy !== null &&
    typeof (item.cloud_policy as Record<string, unknown>).auto_cloud_eligible === "boolean" &&
    typeof (item.cloud_policy as Record<string, unknown>).authorization_source === "string"
  );
}

function parsePage(value: unknown): PagePayload {
  if (typeof value !== "object" || value === null) {
    throw new DocumentApiError("document_list_unavailable");
  }
  const page = value as Record<string, unknown>;
  if (
    !Array.isArray(page.items) ||
    !page.items.every(isDocument) ||
    !Number.isSafeInteger(page.total) ||
    (page.total as number) < 0
  ) {
    throw new DocumentApiError("document_list_unavailable");
  }
  return { items: page.items, total: page.total as number };
}

export const browserDocumentApi: DocumentApi = {
  async listDocuments() {
    try {
      const documents: DocumentPayload[] = [];
      let page = 1;
      let total = 0;
      do {
        const response = await fetch(`/api/documents?page=${page}&page_size=200`, {
          credentials: "same-origin",
        });
        if (!response.ok) throw new DocumentApiError("document_list_unavailable");
        const payload = parsePage(await response.json());
        if (payload.items.length === 0 && documents.length < payload.total) {
          throw new DocumentApiError("document_list_unavailable");
        }
        documents.push(...payload.items);
        total = payload.total;
        page += 1;
      } while (documents.length < total);
      return documents
        .filter((document) =>
          document.current_document_version_id !== null &&
          document.content_id !== null &&
          (document.status === "candidate_pdf" || document.status === "valid_pdf"),
        )
        .map(({ document_id, filename, relative_folder, cloud_policy }) => ({
          document_id,
          filename,
          relative_folder,
          authorization_source: cloud_policy.authorization_source,
        }));
    } catch (error) {
      if (error instanceof DocumentApiError) throw error;
      throw new DocumentApiError("document_list_unavailable");
    }
  },
};
