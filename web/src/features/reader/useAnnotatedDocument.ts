import { useEffect, useRef, useState } from "react";
import { listAnnotations, mergeDocumentIdentity, type AnnotationState } from "../annotations/annotations";
import type { ReaderDocument } from "./PdfReader";

type Identity = Omit<AnnotationState, "items"> & { duplicate?: boolean };

// Each visible PDF owns its identity and mutation lifecycle, independently of
// which pane currently supplies the assistant's context.
export function useAnnotatedDocument(documentId: string | null) {
  const [document, setDocument] = useState<ReaderDocument | null>(null);
  const [annotations, setAnnotations] = useState<AnnotationState | null>(null);
  const [documentError, setDocumentError] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const currentId = useRef(documentId);
  currentId.current = documentId;
  const session = useRef({ live: false, writing: false, revision: 0 });

  useEffect(() => {
    const scope = { live: true, writing: false, revision: 0 };
    session.current = scope;
    const controller = new AbortController();
    setDocument(null); setAnnotations(null); setDocumentError(false); setError(null); setNotice(null); setBusy(false);
    if (documentId) void (async () => {
      try {
        const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}`, { credentials: "same-origin", signal: controller.signal });
        if (!response.ok) throw new Error("document_unavailable");
        const data = await response.json();
        if (!scope.live) return;
        if (data.document_id !== documentId || !data.content_id || !data.current_document_version_id) throw new Error("document_unavailable");
        setDocument({ documentId, documentVersionId: data.current_document_version_id, contentId: data.content_id, filename: data.filename });
      } catch {
        if (scope.live) setDocumentError(true);
        return;
      }
      const revision = scope.revision;
      try {
        const state = await listAnnotations(documentId, controller.signal);
        if (!scope.live || revision !== scope.revision) return;
        if (state.document_id !== documentId) throw new Error("annotation_identity_mismatch");
        setDocument(current => current?.documentId === documentId ? mergeDocumentIdentity(current, state) : current);
        setAnnotations(state);
      } catch (reason) {
        if (scope.live && revision === scope.revision) setError(reason instanceof Error ? reason.message : "annotation_load_failed");
      }
    })();
    return () => { scope.live = false; controller.abort(); };
  }, [documentId, attempt]);

  async function runMutation(action: () => Promise<Identity>) {
    const scope = session.current;
    if (!document || document.documentId !== currentId.current || !scope.live || scope.writing) return;
    const target = document;
    scope.writing = true; scope.revision++;
    setBusy(true); setError(null); setNotice(null);
    const isCurrent = () => scope.live && session.current === scope && currentId.current === target.documentId;
    try {
      const identity = await action();
      if (!isCurrent()) return;
      if (identity.document_id !== target.documentId) throw new Error("annotation_identity_mismatch");
      if (identity.duplicate) setNotice("这段文字已有相同标注，未重复添加。可在笔记中修改颜色或评论。");
      setDocument(mergeDocumentIdentity(target, identity));
      const state = await listAnnotations(target.documentId);
      if (!isCurrent()) return;
      if (state.document_id !== target.documentId) throw new Error("annotation_identity_mismatch");
      setDocument(mergeDocumentIdentity(target, state));
      setAnnotations(state);
    } catch (reason) {
      if (isCurrent()) setError(reason instanceof Error ? reason.message : "annotation_mutation_failed");
    } finally {
      scope.writing = false;
      if (isCurrent()) setBusy(false);
    }
  }

  return { document: document?.documentId === documentId ? document : null,
    annotations: annotations?.document_id === documentId ? annotations : null,
    documentError, error, notice, busy, runMutation, retry: () => setAttempt(n => n + 1) };
}

export type AnnotatedDocument = ReturnType<typeof useAnnotatedDocument>;
