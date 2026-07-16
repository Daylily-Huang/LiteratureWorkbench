import { useCallback, useEffect, useState } from "react";

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

function CurrentReader({ documentId, initialPage }: ReaderLocation) {
  const [document, setDocument] = useState<ReaderDocument | null>(null);
  const [error, setError] = useState(false);
  const [translationSelection, setTranslationSelection] = useState<SelectedText | null>(null);
  const clearTranslationSelection = useCallback(() => setTranslationSelection(null), []);

  useEffect(() => {
    setDocument(null);
    setError(false);
    setTranslationSelection(null);
    if (documentId === null) return;
    const controller = new AbortController();
    void fetch(`/api/documents/${documentId}`, {
      credentials: "same-origin",
      signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) throw new Error("document_metadata_unavailable");
      const value = await response.json() as DocumentResponse;
      if (value.current_document_version_id === null || value.content_id === null) {
        throw new Error("document_metadata_incomplete");
      }
      setDocument({
        documentId: value.document_id,
        documentVersionId: value.current_document_version_id,
        contentId: value.content_id,
        filename: value.filename,
      });
      setError(false);
    }).catch((reason: unknown) => {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) {
        setError(true);
      }
    });
    return () => controller.abort();
  }, [documentId]);

  if (documentId === null) return <><h2>PDF 阅读器</h2><p>请从左侧选择一篇文献</p></>;
  if (error) return <p role="alert">文献信息暂时无法读取</p>;
  if (document === null) return <p>正在读取文献信息</p>;
  return (
    <>
      <PdfReader
        {...document}
        initialPage={initialPage}
        onSelectionClear={clearTranslationSelection}
        onTranslateSelection={setTranslationSelection}
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

export function Workbench() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [location, setLocation] = useState<ReaderLocation>(readerLocation);

  useEffect(() => {
    const updateLocation = () => setLocation(readerLocation());
    window.addEventListener("popstate", updateLocation);
    return () => window.removeEventListener("popstate", updateLocation);
  }, []);

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

  return (
    <div className="workbench">
      <aside aria-label="文献库" className="workbench__library">
        <LibraryPanel
          activeDocumentId={location.documentId}
          onOpenDocument={openDocument}
        />
      </aside>
      <main aria-label="PDF 阅读器" className="workbench__reader">
        <CurrentReader
          documentId={location.documentId}
          initialPage={location.initialPage}
          key={`${location.documentId ?? "none"}:${location.initialPage}`}
        />
      </main>
      <aside aria-label="研究助手" className="workbench__assistant">
        <h2>研究助手</h2>
        <p>RAG/网络检索将在 v0.2</p>
        <button onClick={() => setSettingsOpen(true)} type="button">翻译设置</button>
      </aside>
      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
