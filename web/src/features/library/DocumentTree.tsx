import type { ReadingRecords } from "./readingRecords";
import type { DocumentListItem } from "./documents";

type Props = {
  activeDocumentId: string | null;
  expandAll?: boolean;
  readingRecords?: ReadingRecords;
  onToggleFavorite?(id: string): void;
  documents: DocumentListItem[];
  onOpenDocument(documentId: string): void;
};

type FolderNode = {
  name: string;
  path: string;
  folders: FolderNode[];
  documents: DocumentListItem[];
};

type MutableFolderNode = {
  name: string;
  path: string;
  folders: Map<string, MutableFolderNode>;
  documents: DocumentListItem[];
};

const collator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });

function folderSegments(document: DocumentListItem): string[] {
  if (document.authorization_source === "confirmed_import") return ["直接导入"];
  const folder = document.relative_folder;
  if (folder === "") return ["根目录"];
  if (folder.includes("\\")) return ["根目录"];
  const segments = folder.split("/");
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    return ["根目录"];
  }
  return segments;
}

function freeze(node: MutableFolderNode): FolderNode {
  return {
    name: node.name,
    path: node.path,
    folders: [...node.folders.values()]
      .sort((left, right) => collator.compare(left.name, right.name))
      .map(freeze),
    documents: [...node.documents].sort((left, right) =>
      collator.compare(left.filename, right.filename)),
  };
}

export function buildDocumentTree(documents: DocumentListItem[]): FolderNode[] {
  const root: MutableFolderNode = {
    name: "",
    path: "",
    folders: new Map(),
    documents: [],
  };
  for (const document of documents) {
    let node = root;
    if (document.library_id && document.library_name && document.authorization_source !== "confirmed_import") {
      const key = `library:${document.library_id}`;
      let libraryNode = root.folders.get(key);
      if (!libraryNode) {
        libraryNode = { name: document.library_name, path: key, folders: new Map(), documents: [] };
        root.folders.set(key, libraryNode);
      }
      node = libraryNode;
    }
    for (const segment of folderSegments(document)) {
      const path = node.path === "" ? segment : `${node.path}/${segment}`;
      let child = node.folders.get(segment);
      if (!child) {
        child = { name: segment, path, folders: new Map(), documents: [] };
        node.folders.set(segment, child);
      }
      node = child;
    }
    node.documents.push(document);
  }
  return freeze(root).folders;
}

function containsActive(node: FolderNode, activeDocumentId: string | null): boolean {
  if (activeDocumentId === null) return false;
  return node.documents.some((document) => document.document_id === activeDocumentId) ||
    node.folders.some((folder) => containsActive(folder, activeDocumentId));
}

function documentCount(node: FolderNode): number {
  return node.documents.length + node.folders.reduce((total, folder) => total + documentCount(folder), 0);
}

function Folder({
  readingRecords = {},
  onToggleFavorite,
  expandAll = false,
  activeDocumentId,
  node,
  onOpenDocument,
}: {
  expandAll?: boolean;
  readingRecords?: ReadingRecords;
  onToggleFavorite?(id: string): void;
  activeDocumentId: string | null;
  node: FolderNode;
  onOpenDocument(documentId: string): void;
}) {
  return (
    <details className="document-tree__folder" open={expandAll || containsActive(node, activeDocumentId)}>
      <summary className="document-tree__summary"><span aria-hidden="true">▸</span>{node.name}<small>{documentCount(node)}</small></summary>
      <div className="document-tree__children">
        {node.folders.map((folder) => (
          <Folder
            readingRecords={readingRecords}
            onToggleFavorite={onToggleFavorite}
            expandAll={expandAll}
            activeDocumentId={activeDocumentId}
            key={folder.path}
            node={folder}
            onOpenDocument={onOpenDocument}
          />
        ))}
        {node.documents.map((document) => (
          <div className="document-tree__row" key={document.document_id}>
          <button
            aria-current={activeDocumentId === document.document_id ? "page" : undefined}
            className="document-list__item"
            key={document.document_id}
            onClick={() => onOpenDocument(document.document_id)}
            title={document.filename}
            type="button"
          >
            <span aria-hidden="true" className="document-list__icon" /><span>{document.filename}</span>
          </button>
          {onToggleFavorite && <button className="favorite-button" type="button" aria-label={`${readingRecords[document.document_id]?.favorite ? "取消收藏" : "收藏"} ${document.filename}`} aria-pressed={readingRecords[document.document_id]?.favorite ?? false} onClick={() => onToggleFavorite(document.document_id)}>{readingRecords[document.document_id]?.favorite ? "★" : "☆"}</button>}
          </div>
        ))}
      </div>
    </details>
  );
}

export function DocumentTree({ activeDocumentId, documents, onOpenDocument, expandAll = false, readingRecords = {}, onToggleFavorite }: Props) {
  return (
    <nav aria-label="文献列表" className="document-list">
      {buildDocumentTree(documents).map((folder) => (
        <Folder
          readingRecords={readingRecords}
          onToggleFavorite={onToggleFavorite}
          expandAll={expandAll}
          activeDocumentId={activeDocumentId}
          key={folder.path}
          node={folder}
          onOpenDocument={onOpenDocument}
        />
      ))}
    </nav>
  );
}
