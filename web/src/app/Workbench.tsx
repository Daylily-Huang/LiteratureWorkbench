import { useEffect, useState } from "react";

import { LibraryPanel } from "../features/library/LibraryPanel";
import { PdfReader, type ReaderDocument } from "../features/reader/PdfReader";


type DocumentResponse = {
  document_id: string;
  current_document_version_id: string | null;
  content_id: string | null;
  filename: string;
};


function CurrentReader() {
  const params = new URLSearchParams(window.location.search);
  const documentId = params.get("document");
  const initialPage = Number(params.get("page") ?? "1");
  const [document, setDocument] = useState<ReaderDocument | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
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

  if (documentId === null) return <h2>PDF 阅读器</h2>;
  if (error) return <p role="alert">文献信息暂时无法读取</p>;
  if (document === null) return <p>正在读取文献信息</p>;
  return <PdfReader {...document} initialPage={Number.isInteger(initialPage) ? initialPage : 1} />;
}

export function Workbench() {
  return (
    <div className="workbench">
      <aside aria-label="文献库" className="workbench__library">
        <LibraryPanel />
      </aside>
      <main aria-label="PDF 阅读器" className="workbench__reader">
        <CurrentReader />
      </main>
      <aside aria-label="研究助手" className="workbench__assistant">
        <h2>研究助手</h2>
      </aside>
    </div>
  );
}
