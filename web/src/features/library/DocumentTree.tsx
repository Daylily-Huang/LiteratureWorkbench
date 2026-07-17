import type { DocumentListItem } from "./documents";

type Props = {
  activeDocumentId: string | null;
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

function Folder({
  activeDocumentId,
  node,
  onOpenDocument,
}: {
  activeDocumentId: string | null;
  node: FolderNode;
  onOpenDocument(documentId: string): void;
}) {
  return (
    <details className="document-tree__folder" open={containsActive(node, activeDocumentId)}>
      <summary className="document-tree__summary"><span aria-hidden="true">▸</span>{node.name}<small>{node.documents.length + node.folders.length}</small></summary>
      <div className="document-tree__children">
        {node.folders.map((folder) => (
          <Folder
            activeDocumentId={activeDocumentId}
            key={folder.path}
            node={folder}
            onOpenDocument={onOpenDocument}
          />
        ))}
        {node.documents.map((document) => (
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
        ))}
      </div>
    </details>
  );
}

export function DocumentTree({ activeDocumentId, documents, onOpenDocument }: Props) {
  return (
    <nav aria-label="文献列表" className="document-list">
      {buildDocumentTree(documents).map((folder) => (
        <Folder
          activeDocumentId={activeDocumentId}
          key={folder.path}
          node={folder}
          onOpenDocument={onOpenDocument}
        />
      ))}
    </nav>
  );
}
