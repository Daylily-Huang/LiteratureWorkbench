import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { AnnotationPanel } from "../features/annotations/AnnotationPanel";
import {
  createAnnotation,
  deleteAnnotation,
  listAnnotations,
  mergeDocumentIdentity,
  mergeSelectionIdentity,
  restoreAnnotationBaseline,
  undoAnnotation,
  updateAnnotation,
  type AnnotationState,
  type PdfAnnotation,
} from "../features/annotations/annotations";
import { DashboardHome } from "../features/dashboard/DashboardHome";
import { LibraryPanel } from "../features/library/LibraryPanel";
import type { DocumentListItem } from "../features/library/documents";
import type { PdfRect } from "../features/reader/CitationHighlight";
import { PdfReader, type CitationJump, type ReaderDocument } from "../features/reader/PdfReader";
import type { SelectedText } from "../features/reader/selection";
import { AssistantPanel, type LibraryCitation, type ResearchCitation } from "../features/research/AssistantPanel";
import { SettingsDialog } from "../features/settings/SettingsDialog";
import { DeepSeekBilling } from "../features/settings/DeepSeekBilling";
import { TranslationPopover } from "../features/translation/TranslationPopover";
import { ConfirmDialog } from "../features/ui/ConfirmDialog";


type DocumentResponse = {
  document_id: string;
  current_document_version_id: string | null;
  content_id: string | null;
  filename: string;
};

type ReaderLocation = { documentId: string | null; initialPage: number };
type ThemePreference = "system" | "light" | "dark";
type Confirmation =
  | { kind: "delete"; annotation: PdfAnnotation }
  | { kind: "restore" };


function readerLocation(): ReaderLocation {
  const params = new URLSearchParams(window.location.search);
  const documentId = params.get("document") ?? params.get("documentId");
  const rawPage = Number(params.get("page"));
  const initialPage = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;
  return { documentId, initialPage };
}

function updateReaderLocation(location: ReaderLocation) {
  const params = new URLSearchParams();
  if (location.documentId !== null) params.set("document", location.documentId);
  if (location.initialPage >= 1) params.set("page", String(location.initialPage));
  const query = params.toString();
  const next = query.length === 0 ? "/" : `/?${query}`;
  window.history.pushState(null, "", next);
}


export function Workbench() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [location, setLocation] = useState<ReaderLocation>(readerLocation);
  const [document, setDocument] = useState<ReaderDocument | null>(null);
  const [documentList, setDocumentList] = useState<DocumentListItem[]>([]);
  const [documentError, setDocumentError] = useState(false);
  const [translationSelection, setTranslationSelection] = useState<SelectedText | null>(null);
  const [annotations, setAnnotations] = useState<AnnotationState | null>(null);
  const [annotationBusy, setAnnotationBusy] = useState(false);
  const [annotationError, setAnnotationError] = useState<string | null>(null);
  const [activeAnnotationId, setActiveAnnotationId] = useState<string | null>(null);
  const [requestedPage, setRequestedPage] = useState<number | undefined>();
  const [citationHighlight, setCitationHighlight] = useState<CitationJump | null>(null);

  // A library-scope citation pointing at another document: applied once the
  // target document finishes loading.
  const [pendingCitation, setPendingCitation] = useState<{
    documentId: string;
    page: number;
    rects: readonly PdfRect[] | number[][];
  } | null>(null);
  const [draftQuestion, setDraftQuestion] = useState("");
  const [focusMode, setFocusMode] = useState(false);
  const [mobilePanel, setMobilePanel] = useState<"library" | "assistant" | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [theme, setTheme] = useState<ThemePreference>(() => {
    const stored = localStorage.getItem("lw-theme");
    return stored === "light" || stored === "dark" ? stored : "system";
  });
  const [resolvedTheme, setResolvedTheme] = useState<"light" | "dark">("light");
  const clearTranslationSelection = useCallback(() => setTranslationSelection(null), []);

  useEffect(() => {
    const updateLocation = () => setLocation(readerLocation());
    window.addEventListener("popstate", updateLocation);
    return () => window.removeEventListener("popstate", updateLocation);
  }, []);

  useEffect(() => {
    const media = typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-color-scheme: dark)")
      : null;
    const resolve = () => setResolvedTheme(theme === "system" ? media?.matches ? "dark" : "light" : theme);
    resolve();
    media?.addEventListener?.("change", resolve);
    return () => media?.removeEventListener?.("change", resolve);
  }, [theme]);

  useEffect(() => {
    setDocument(null);
    setDocumentError(false);
    setTranslationSelection(null);
    setAnnotations(null);
    setAnnotationError(null);
    setActiveAnnotationId(null);
    setRequestedPage(undefined);
    setCitationHighlight(null);
    setPendingCitation((prev) => (prev?.documentId === location.documentId ? prev : null));

    if (location.documentId === null) return undefined;
    const controller = new AbortController();

    void fetch(`/api/documents/${location.documentId}`, {
      credentials: "same-origin",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("document_unavailable");
        return response.json() as Promise<DocumentResponse>;
      })
      .then((data) => {
        if (
          data.current_document_version_id === null ||
          data.content_id === null
        ) {
          throw new Error("document_not_ready");
        }
        setDocument({
          documentId: data.document_id,
          documentVersionId: data.current_document_version_id,
          contentId: data.content_id,
          filename: data.filename,
        });
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setDocumentError(true);
      });

    return () => controller.abort();
  }, [location.documentId]);

  useEffect(() => {
    if (document === null) {
      setAnnotations(null);
      return undefined;
    }
    const controller = new AbortController();
    void listAnnotations(document.documentId, controller.signal)
      .then((state) => {
        setAnnotations(state);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setAnnotationError(error instanceof Error ? error.message : "annotation_load_failed");
      });
    return () => controller.abort();
  }, [document]);

  const activeDocumentIdRef = useRef<string | null>(location.documentId);
  activeDocumentIdRef.current = location.documentId;
  const annotationMutationGenerationRef = useRef(0);

  async function runMutation(action: () => Promise<{
    current_document_version_id: string;
    content_id: string;
    document_id: string;
  }>) {
    if (document === null) return;
    const targetDocId = document.documentId;
    const currentGen = ++annotationMutationGenerationRef.current;
    setAnnotationBusy(true);
    setAnnotationError(null);
    try {
      const identity = await action();
      if (
        activeDocumentIdRef.current !== targetDocId ||
        annotationMutationGenerationRef.current !== currentGen
      ) {
        return;
      }
      const next = mergeDocumentIdentity(document, identity);
      setDocument(next);
      const annotationsState = await listAnnotations(next.documentId);
      if (
        activeDocumentIdRef.current !== targetDocId ||
        annotationMutationGenerationRef.current !== currentGen
      ) {
        return;
      }
      setAnnotations(annotationsState);
      setTranslationSelection((current) => (
        current?.documentId === identity.document_id
          ? mergeSelectionIdentity(current, identity)
          : null
      ));
    } catch (reason: unknown) {
      if (
        activeDocumentIdRef.current === targetDocId &&
        annotationMutationGenerationRef.current === currentGen
      ) {
        setAnnotationError(reason instanceof Error ? reason.message : "annotation_mutation_failed");
      }
    } finally {
      if (annotationMutationGenerationRef.current === currentGen) {
        setAnnotationBusy(false);
      }
    }
  }

  function openDocument(documentId: string | null) {
    const next: ReaderLocation = { documentId, initialPage: 1 };
    setLocation(next);
    updateReaderLocation(next);
    setMobilePanel(null);
  }

  function goHome() {
    openDocument(null);
  }

  function jumpToPage(page: number) {
    setRequestedPage(page);
  }

  function jumpToAnnotation(annotation: PdfAnnotation) {
    setActiveAnnotationId(annotation.annotation_id);
    jumpToPage(annotation.page);
  }

  function jumpToCitation(citation: ResearchCitation) {
    if (document === null) return;
    setCitationHighlight({
      documentId: document.documentId,
      page: citation.physical_page,
      rects: citation.rects,
      nonce: Date.now(),
    });
    jumpToPage(citation.physical_page);
  }

  function jumpToLibraryCitation(citation: LibraryCitation) {
    if (document !== null && document.documentId === citation.document_id) {
      // Citation lands in the currently-open document: flash straight away.
      jumpToCitation(citation);
      return;
    }
    // Citation points at a different document: record the destination and
    // navigate.  The effect below will apply the highlight and jump once the
    // reader finishes mounting the new document.
    setPendingCitation({
      documentId: citation.document_id,
      page: citation.physical_page,
      rects: citation.rects,
    });
    openDocument(citation.document_id);
  }

  // Once the target document of a cross-document citation jump mounts, apply
  // the highlight and jump to the cited page.
  useEffect(() => {
    if (document === null || pendingCitation === null) return;
    if (pendingCitation.documentId !== document.documentId) return;
    setCitationHighlight({
      documentId: document.documentId,
      page: pendingCitation.page,
      rects: pendingCitation.rects,
      nonce: Date.now(),
    });
    jumpToPage(pendingCitation.page);
    setPendingCitation(null);
  }, [document, pendingCitation]);

  // Citation highlight auto-dismisses after 5 s; a new jump restarts it.
  useEffect(() => {
    if (citationHighlight === null) return undefined;
    const timer = window.setTimeout(() => setCitationHighlight(null), 5000);
    return () => window.clearTimeout(timer);
  }, [citationHighlight]);

  let reader: ReactNode;
  if (location.documentId === null) {
    reader = (
      <DashboardHome
        documents={documentList}
        onAskQuestion={(q) => {
          setDraftQuestion(q);
          if (focusMode) setFocusMode(false);
          setMobilePanel("assistant");
        }}
        onOpenDocument={openDocument}
        onTriggerImport={() => {
          if (focusMode) setFocusMode(false);
          setMobilePanel("library");
        }}
      />
    );
  } else if (documentError) {
    reader = <div className="workbench-error-wrap"><p role="alert">文献信息暂时无法读取</p></div>;
  } else if (document === null) {
    reader = <div className="workbench-loading-wrap"><span className="workbench-loading-spinner" /><p>正在读取文献信息…</p></div>;
  } else {
    reader = (
      <>
        <PdfReader
          {...document}
          activeAnnotationId={activeAnnotationId}
          annotationBusy={annotationBusy}
          annotations={annotations?.items ?? []}
          citationHighlight={citationHighlight}
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
    <div className="workbench-shell" data-theme={resolvedTheme}>
      <header className="topbar">
        <button
          aria-label="返回文献工作台首页"
          className="topbar__brand-button"
          onClick={goHome}
          type="button"
        >
          <span aria-hidden="true" className="topbar__mark">📖</span>
          <div className="topbar__title-wrap">
            <strong>Literature Workbench</strong>
            <span className="topbar__subtitle">文献工作台</span>
          </div>
        </button>

        <div className="topbar__document" title={document?.filename ?? "尚未选择文献"}>
          {document ? (
            <div className="topbar__doc-badge">
              <span className="topbar__doc-tag">PDF</span>
              <span className="topbar__doc-name">{document.filename}</span>
            </div>
          ) : (
            <span className="topbar__motto">专注当下，积累复利。你正在构建自己的学术知识体系。</span>
          )}
        </div>

        <div className="topbar__actions">
          <button
            aria-expanded={mobilePanel === "library"}
            className="topbar__button mobile-only"
            onClick={() => setMobilePanel((value) => value === "library" ? null : "library")}
            type="button"
          >
            文献库
          </button>
          <button
            aria-expanded={mobilePanel === "assistant"}
            className="topbar__button mobile-only"
            onClick={() => setMobilePanel((value) => value === "assistant" ? null : "assistant")}
            type="button"
          >
            助手与笔记
          </button>
          <DeepSeekBilling />
          <button
            aria-pressed={focusMode}
            className={`topbar__button topbar__button--focus ${focusMode ? "topbar__button--active" : ""}`}
            onClick={() => setFocusMode((value) => !value)}
            type="button"
          >
            {focusMode ? "退出专注" : "专注阅读"}
          </button>
          <button
            aria-label={`切换为${resolvedTheme === "dark" ? "浅色" : "深色"}主题`}
            className="icon-button"
            onClick={() => {
              const next = resolvedTheme === "dark" ? "light" : "dark";
              localStorage.setItem("lw-theme", next);
              setTheme(next);
            }}
            title={resolvedTheme === "dark" ? "切换为浅色主题" : "切换为深色主题"}
            type="button"
          >
            {resolvedTheme === "dark" ? "☀" : "◐"}
          </button>
          <button
            aria-label="打开设置"
            className="icon-button"
            onClick={() => setSettingsOpen(true)}
            title="设置"
            type="button"
          >
            ⚙
          </button>
        </div>
      </header>

      <div
        className={`workbench${focusMode ? " workbench--focus" : ""}${mobilePanel === "library" ? " workbench--show-library" : ""}${mobilePanel === "assistant" ? " workbench--show-assistant" : ""}`}
      >
        {mobilePanel !== null && (
          <button
            aria-label="关闭侧栏"
            className="workbench__scrim"
            onClick={() => setMobilePanel(null)}
            type="button"
          />
        )}
        <aside aria-label="文献库" className="workbench__library">
          <LibraryPanel
            activeDocumentId={location.documentId}
            onDocumentsLoaded={setDocumentList}
            onOpenDocument={openDocument}
          />
        </aside>
        <main aria-label="PDF 阅读器" className="workbench__reader">
          {reader}
        </main>
        <aside aria-label="研究助手" className="workbench__assistant">
          <AssistantPanel
            document={document}
            draftQuestion={draftQuestion}
            onDraftQuestionConsumed={() => setDraftQuestion("")}
            onJumpToCitation={jumpToCitation}
            onJumpToLibraryCitation={jumpToLibraryCitation}
            onOpenSettings={() => setSettingsOpen(true)}
          />
          <AnnotationPanel
            busy={annotationBusy}
            document={document}
            error={annotationError}
            onDelete={(annotation) => {
              if (document !== null) setConfirmation({ kind: "delete", annotation });
            }}
            onJump={jumpToAnnotation}
            onRestore={() => {
              if (document !== null) setConfirmation({ kind: "restore" });
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
      </div>
      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
      {confirmation?.kind === "delete" && (
        <ConfirmDialog
          confirmLabel="删除批注"
          description="这会从原 PDF 中删除该条本站批注，可以使用撤销上次恢复。"
          label="删除批注"
          onCancel={() => setConfirmation(null)}
          onConfirm={() => {
            const annotation = confirmation.annotation;
            setConfirmation(null);
            if (document !== null) void runMutation(() => deleteAnnotation(document, annotation.annotation_id));
          }}
        />
      )}
      {confirmation?.kind === "restore" && document !== null && (
        <ConfirmDialog
          confirmLabel="恢复原始 PDF"
          description={`此操作会移除本站写入的全部批注。请输入 ${document.filename} 继续。`}
          expectedText={document.filename}
          label="恢复原始 PDF"
          onCancel={() => setConfirmation(null)}
          onConfirm={() => {
            setConfirmation(null);
            void runMutation(() => restoreAnnotationBaseline(document.documentId, document.filename));
          }}
        />
      )}
    </div>
  );
}
