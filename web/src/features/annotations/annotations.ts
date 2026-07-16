import type { ReaderDocument } from "../reader/PdfReader";
import type { SelectedText } from "../reader/selection";


export type PdfAnnotation = {
  annotation_id: string;
  page: number;
  type: string;
  owned: boolean;
  read_only: boolean;
  comment: string;
  selected_text: string;
  subject: string;
  color: number[];
  quads: number[][];
};

export type AnnotationState = {
  items: PdfAnnotation[];
  document_id: string;
  current_document_version_id: string;
  content_id: string;
};

type IdentityResponse = {
  document_id: string;
  current_document_version_id: string;
  content_id: string;
};


async function csrf(): Promise<string> {
  const response = await fetch("/api/session/current", { credentials: "same-origin" });
  if (!response.ok) throw new Error("session_unavailable");
  return (await response.json() as { csrf_token: string }).csrf_token;
}


async function write<T>(url: string, method: string, body?: object): Promise<T> {
  const token = await csrf();
  const response = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-LW-CSRF": token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const value = await response.json() as T & { code?: string };
  if (!response.ok) throw new Error(value.code ?? "annotation_request_failed");
  return value;
}


export async function listAnnotations(documentId: string): Promise<AnnotationState> {
  const response = await fetch(`/api/documents/${documentId}/annotations`, {
    credentials: "same-origin",
  });
  const value = await response.json() as AnnotationState & { code?: string };
  if (!response.ok) throw new Error(value.code ?? "annotation_list_failed");
  return value;
}


export async function createAnnotation(
  selected: SelectedText,
  type: "highlight" | "underline" | "comment",
  color: string,
  comment = "",
): Promise<IdentityResponse> {
  if (selected.pdfQuads.length === 0) throw new Error("annotation_coordinates_unavailable");
  return write(`/api/documents/${selected.documentId}/annotations`, "POST", {
    document_version_id: selected.documentVersionId,
    content_id: selected.contentId,
    page: selected.pdfPage,
    type,
    quads: selected.pdfQuads,
    color,
    comment,
  });
}


export async function updateAnnotation(
  document: ReaderDocument,
  annotationId: string,
  changes: { color?: string; comment?: string },
): Promise<IdentityResponse> {
  return write(
    `/api/documents/${document.documentId}/annotations/${encodeURIComponent(annotationId)}`,
    "PATCH",
    {
      document_version_id: document.documentVersionId,
      content_id: document.contentId,
      ...changes,
    },
  );
}


export async function deleteAnnotation(
  document: ReaderDocument,
  annotationId: string,
): Promise<IdentityResponse> {
  return write(
    `/api/documents/${document.documentId}/annotations/${encodeURIComponent(annotationId)}`,
    "DELETE",
    {
      document_version_id: document.documentVersionId,
      content_id: document.contentId,
    },
  );
}


export async function undoAnnotation(documentId: string): Promise<IdentityResponse> {
  return write(`/api/documents/${documentId}/annotations/undo`, "POST");
}


export async function restoreAnnotationBaseline(
  documentId: string,
  filename: string,
): Promise<IdentityResponse> {
  return write(`/api/documents/${documentId}/annotations/restore-baseline`, "POST", {
    confirm_filename: filename,
  });
}


export function mergeDocumentIdentity(
  document: ReaderDocument,
  identity: IdentityResponse,
): ReaderDocument {
  return {
    ...document,
    documentVersionId: identity.current_document_version_id,
    contentId: identity.content_id,
  };
}


export function mergeSelectionIdentity(
  selected: SelectedText,
  identity: IdentityResponse,
): SelectedText {
  if (selected.documentId !== identity.document_id) return selected;
  return {
    ...selected,
    documentVersionId: identity.current_document_version_id,
    contentId: identity.content_id,
  };
}
