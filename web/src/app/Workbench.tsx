import { useCallback, useEffect, useState, type ReactNode } from "react";

import { AnnotationPanel } from "../features/annotations/AnnotationPanel";
import {
  createAnnotation,
  deleteAnnotation,
  listAnnotations,
  mergeDocumentIdentity,
  restoreAnnotationBaseline,
  undoAnnotation,
  updateAnnotation,
  type AnnotationState,
  type PdfAnnotation,
} from "../features/annotations/annotations";
import { LibraryPanel } from "../features/library/LibraryPanel";
import { PdfReader, type ReaderDocument } from "../features/reader/PdfReader";
import type { SelectedText } from "../features/reader/selection";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { TranslationPopover } from "../features/translation/TranslationPopover";


type DocumentResponse = {
  document_id: string;
  current_document_version_id: string | null;
  content_id: string | null;
  filename: string;
};

type ReaderLocation = { documentId: string | null; initialPage: number };


function readerLocation(): ReaderLocation {
  const params = new URLSearchParams(window.location.search);
  const page = Number(params.get("page") ?? "1");
  return {
    documentId: params.get("document"),
    initialPage: Number.isInteger(page) && page > 0 ? page : 1,
  };
}


function annotationErrorMessage(reason: unknown): string {
  const code = reason instanceof Error ? reason.message : "annotation_request_failed";
  const known: Record<string, string> = {
    annotation_coordinates_unavailable: "当前页面坐标尚未就绪，请重新选择文字",
    annotation_document_version_conflict: "PDF 已变化，请重新选择文字",
    annotation_external_change: "检测到 PDF 被其他程序修改，已停止写入",
    annotation_pdf_not_incremental: "此 PDF 暂不支持安全的原位批注",
    annotation_read_only: "历史批注只读，不能修改",
    annotation_undo_unavailable: "没有可撤销的本站批注操作",
    annotation_baseline_missing: "尚未创建原始版本备份",
  };
  return known[code] ?? "PDF 批注操作失败，原文件未被覆盖";
}


export function Workbench() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [location, setLocation] = useState<ReaderLocation>(readerLocation);
  const [document, setDocument] = useState<ReaderDocument | null>(null);
  const [documentError, setDocumentError] = useState(false);
  const [translationSelection, setTranslationSelection] = useState<SelectedText | null>(null);
  const [annotations, setAnnotations] = useState<AnnotationState | null>(null);
  const [annotationBusy, setAnnotationBusy] = useState(false);
  const [annotationError, setAnnotationError] = useState<string | null>(null);
  const [activeAnnotationId, setActiveAnnotationId] = useState<string | null>(null);
  const [requestedPage, setRequestedPage] = useState<number | undefined>();
  const clearTranslationSelection = useCallback(() => setTranslationSelection(null), []);

  useEffect(() => {
    const updateLocation = () => setLocation(readerLocation());
    window.addEventListener("popstate", updateLocation);
    return () => window.removeEventListener("popstate", updateLocation);
  }, []);

  useEffect(() => {
    setDocument(null);
    setDocumentError(false);
    setTranslationSelection(null);
    setAnnotations(null);
    setAnnotationError(null);
    setActiveAnnotationId(null);
    setRequestedPage(undefined);
    if (location.documentId === null) return;
    const controller = new AbortController();
    void fetch(`/api/documents/${location.documentId}`, {
      credentials: "same-origin",
      signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) throw new Error("document_metadata_unavailable");
      const value = await response.json() as DocumentResponse;
      if (value.current_document_version_id === null || value.content_id === null) {
        throw new Error("document_metadata_incomplete");
      }
      const next = {
        documentId: value.document_id,
        documentVersionId: value.current_document_version_id,
        contentId: value.content_id,
        filename: value.filename,
      };
      setDocument(next);
      setAnnotations(await listAnnotations(next.documentId));
      setDocumentError(false);
    }).catch((reason: unknown) => {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) {
        setDocumentError(true);
      }
    });
    return () => controller.abort();
  }, [location.documentId]);

  function openDocument(documentId: string) {
    const url = new URL(window.location.href);
    url.pathname = "/";
    url.search = "";
    url.searchParams.set("document", documentId);
    url.searchParams.set("page", "1");
    url.hash = "";
    window.history.pushState({}, "", url);
    setLocation({ documentId, initialPage: 1 });
  }

  async function runMutation(action: () => Promise<{
    current_document_version_id: string;
    content_id: string;
    document_id: string;
  }>) {
    if (document === null) return;
    setAnnotationBusy(true);
    setAnnotationError(null);
    try {
      const identity = await action();
      const next = mergeDocumentIdentity(document, identity);
      setDocument(next);
      setAnnotations(await listAnnotations(next.documentId));
      setTranslationSelection(null);
    } catch (reason: unknown) {
      setAnnotationError(annotationErrorMessage(reason));
    } finally {
      setAnnotationBusy(false);
    }
  }

  function jumpToAnnotation(annotation: PdfAnnotation) {
    setRequestedPage(annotation.page);
    setActiveAnnotationId(annotation.annotation_id);
    window.setTimeout(() => {
      globalThis.document
        .getElementById(`annotation-${annotation.annotation_id}`)
        ?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }, 0);
  }

  let reader: ReactNode;
  if (location.documentId === null) {
    reader = <><h2>PDF 阅读器</h2><p>请从左侧选择一篇文献</p></>;
  } else if (documentError) {
    reader = <p role="alert">文献信息暂时无法读取</p>;
  } else if (document === null) {
    reader = <p>正在读取文献信息</p>;
  } else {
    reader = (
      <>
        <PdfReader
          {...document}
          activeAnnotationId={activeAnnotationId}
          annotationBusy={annotationBusy}
          annotations={annotations?.items ?? []}
          initialPage={location.initialPage}
          onAnnotateSelection={(selected, type, color, comment) => {
            void runMutation(() => createAnnotation(selected, type, color, comment));
          }}
          onOpenAnnotation={(annotationId) => {
            const annotation = annotations?.items.find((item) => item.annotation_id === annotationId);
            if (annotation !== undefined) jumpToAnnotation(annotation);
          }}
          onSelectionClear={clearTranslationSelection}
          onTranslateSelection={setTranslationSelection}
          requestedPage={requestedPage}
        />
        {translationSelection !== null && (
          <TranslationPopover
            onClose={() => setTranslationSelection(null)}
            selected={translationSelection}
          />
        )}
      </>
    );
  }

  return (
    <div className="workbench">
      <aside aria-label="文献库" className="workbench__library">
        <LibraryPanel activeDocumentId={location.documentId} onOpenDocument={openDocument} />
      </aside>
      <main aria-label="PDF 阅读器" className="workbench__reader">{reader}</main>
      <aside aria-label="研究助手" className="workbench__assistant">
        <h2>研究助手</h2>
        <p>RAG/网络检索将在 v0.2</p>
        <button onClick={() => setSettingsOpen(true)} type="button">翻译设置</button>
        <AnnotationPanel
          busy={annotationBusy}
          document={document}
          error={annotationError}
          onDelete={(annotation) => {
            if (document !== null && window.confirm("确定删除这条本站批注吗？")) {
              void runMutation(() => deleteAnnotation(document, annotation.annotation_id));
            }
          }}
          onJump={jumpToAnnotation}
          onRestore={() => {
            if (document === null) return;
            const value = window.prompt(`输入文件名以恢复原始 PDF：\n${document.filename}`);
            if (value === document.filename) {
              void runMutation(() => restoreAnnotationBaseline(document.documentId, value));
            }
          }}
          onUndo={() => {
            if (document !== null) void runMutation(() => undoAnnotation(document.documentId));
          }}
          onUpdate={(annotation, changes) => {
            if (document !== null) {
              void runMutation(() => updateAnnotation(document, annotation.annotation_id, changes));
            }
          }}
          state={annotations}
        />
      </aside>
      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
