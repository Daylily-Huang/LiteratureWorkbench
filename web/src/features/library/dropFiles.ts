export type ImportStatus =
  | "receiving"
  | "uploaded"
  | "validating"
  | "ready_to_promote"
  | "completed"
  | "failed";

export type ImportTask = {
  intent_id: string;
  file_name: string;
  status: ImportStatus;
  document_id: string | null;
  error_code: string | null;
  poll_after_ms: number | null;
};

export type ImportErrorCode =
  | "import_failed"
  | "invalid_pdf_extension"
  | "content_length_mismatch"
  | "upload_size_mismatch"
  | "invalid_pdf_signature"
  | "import_too_large"
  | "unsupported_media_type"
  | "import_not_found";

const allowedCodes = new Set<ImportErrorCode>([
  "invalid_pdf_extension",
  "content_length_mismatch",
  "upload_size_mismatch",
  "invalid_pdf_signature",
  "import_too_large",
  "unsupported_media_type",
  "import_not_found",
]);

export class ImportApiError extends Error {
  constructor(readonly code: ImportErrorCode) {
    super(code);
  }
}

async function responseError(response: Response): Promise<ImportApiError> {
  try {
    const payload = (await response.json()) as { code?: unknown };
    if (typeof payload.code === "string" && allowedCodes.has(payload.code as ImportErrorCode)) {
      return new ImportApiError(payload.code as ImportErrorCode);
    }
  } catch {
    // A fixed local code keeps server/network details out of the UI.
  }
  return new ImportApiError("import_failed");
}

export type ImportIntent = {
  intent_id: string;
  upload_token: string;
  expires_at: string;
};

export type ImportApi = {
  createImportIntent(file: File): Promise<ImportIntent>;
  uploadImport(intent: ImportIntent, file: File): Promise<ImportTask>;
  getImport(intentId: string): Promise<ImportTask>;
  listActiveImports(): Promise<ImportTask[]>;
};

async function writeHeaders(contentType: string): Promise<Record<string, string>> {
  const response = await fetch("/api/session/current", { credentials: "same-origin" });
  if (!response.ok) throw new Error("session_required");
  const session = (await response.json()) as { csrf_token: string };
  return { "Content-Type": contentType, "X-LW-CSRF": session.csrf_token };
}

export const browserImportApi: ImportApi = {
  async createImportIntent(file) {
    const response = await fetch("/api/imports/intents", {
      method: "POST",
      credentials: "same-origin",
      headers: await writeHeaders("application/json"),
      body: JSON.stringify({
        original_name: file.name,
        size: file.size,
        media_type: "application/pdf",
      }),
    });
    if (!response.ok) throw await responseError(response);
    return response.json() as Promise<ImportIntent>;
  },
  async uploadImport(intent, file) {
    const response = await fetch(`/api/imports/${intent.intent_id}/content`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        ...(await writeHeaders("application/pdf")),
        "X-LW-Upload-Token": intent.upload_token,
      },
      body: file,
    });
    if (!response.ok) throw await responseError(response);
    return response.json() as Promise<ImportTask>;
  },
  async getImport(intentId) {
    const response = await fetch(`/api/imports/${intentId}`, { credentials: "same-origin" });
    if (!response.ok) throw await responseError(response);
    return response.json() as Promise<ImportTask>;
  },
  async listActiveImports() {
    try {
      const response = await fetch("/api/imports?active=true", { credentials: "same-origin" });
      if (!response.ok) return [];
      const result = (await response.json()) as { items: ImportTask[] };
      return result.items;
    } catch {
      return [];
    }
  },
};
