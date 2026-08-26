import { useEffect, useRef, useState } from "react";

import { ImportConfirmDialog } from "./ImportConfirmDialog";
import { DocumentTree } from "./DocumentTree";
import {
  browserDocumentApi,
  type DocumentApi,
  type DocumentListItem,
} from "./documents";
import {
  ImportApiError,
  browserImportApi,
  type ImportApi,
  type ImportErrorCode,
  type ImportTask,
} from "./dropFiles";

type Props = {
  activeDocumentId?: string | null;
  documentApi?: DocumentApi;
  documentRefreshMs?: number;
  importApi?: ImportApi;
  onOpenDocument?: (documentId: string) => void;
  onDocumentsLoaded?: (documents: DocumentListItem[]) => void;
};

type LibraryCategory = "all" | "unclassified";

function sameDocuments(left: DocumentListItem[], right: DocumentListItem[]): boolean {
  return left.length === right.length && left.every((item, index) => {
    const other = right[index];
    return other !== undefined
      && item.document_id === other.document_id
      && item.filename === other.filename
      && item.relative_folder === other.relative_folder
      && item.authorization_source === other.authorization_source;
  });
}

export function LibraryPanel({
  activeDocumentId = null,
  documentApi = browserDocumentApi,
  documentRefreshMs = 3000,
  importApi = browserImportApi,
  onOpenDocument = () => undefined,
  onDocumentsLoaded,
}: Props) {
  const [selected, setSelected] = useState<File | null>(null);
  const [unsupported, setUnsupported] = useState(false);
  const [tasks, setTasks] = useState<ImportTask[]>([]);
  const [documents, setDocuments] = useState<DocumentListItem[]>([]);
  const [documentsFailed, setDocumentsFailed] = useState(false);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<LibraryCategory>("all");
  const [pollFailures, setPollFailures] = useState<Set<string>>(new Set());
  const input = useRef<HTMLInputElement>(null);
  const confirming = useRef(false);

  function putDocuments(next: DocumentListItem[]) {
    setDocuments((current) => (sameDocuments(current, next) ? current : next));
  }

  useEffect(() => {
    onDocumentsLoaded?.(documents);
  }, [documents, onDocumentsLoaded]);

  function putTask(task: ImportTask) {
    setTasks((current) => [task, ...current.filter((item) => item.intent_id !== task.intent_id)]);
  }

  async function refreshDocuments() {
    try {
      const items = await documentApi.listDocuments();
      putDocuments(items);
      setDocumentsFailed(false);
    } catch {
      setDocumentsFailed(true);
    }
  }

  function acceptNewTask(previous: ImportTask, next: ImportTask) {
    putTask(next);
    if (
      previous.status !== "completed" &&
      next.status === "completed" &&
      next.document_id !== null
    ) {
      void refreshDocuments();
      onOpenDocument(next.document_id);
    }
  }

  useEffect(() => {
    let stopped = false;
    let timer: number | null = null;
    const pollDocuments = async () => {
      try {
        const items = await documentApi.listDocuments();
        if (stopped) return;
        putDocuments(items);
        setDocumentsFailed(false);
      } catch {
        if (!stopped) setDocumentsFailed(true);
      } finally {
        if (!stopped) {
          timer = window.setTimeout(() => void pollDocuments(), documentRefreshMs);
        }
      }
    };
    void pollDocuments();
    return () => {
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [documentApi, documentRefreshMs]);

  useEffect(() => {
    let stopped = false;
    void importApi.listActiveImports().then((tasks) => {
      if (!stopped) setTasks(tasks);
    });
    return () => { stopped = true; };
  }, [importApi]);

  useEffect(() => {
    let stopped = false;
    const timers: number[] = [];
    for (const task of tasks) {
      if (task.poll_after_ms === null) continue;
      let failureCount = 0;
      const poll = () => {
        if (stopped) return;
        void importApi.getImport(task.intent_id).then(
          (next) => {
            if (stopped) return;
            failureCount = 0;
            setPollFailures((current) => {
              const copy = new Set(current);
              copy.delete(task.intent_id);
              return copy;
            });
            acceptNewTask(task, next);
          },
          (reason: unknown) => {
            if (stopped) return;
            if (reason instanceof ImportApiError && reason.code === "import_not_found") {
              putTask({
                ...task,
                status: "failed",
                error_code: "import_not_found",
                poll_after_ms: null,
              });
              setPollFailures((current) => {
                const copy = new Set(current);
                copy.delete(task.intent_id);
                return copy;
              });
              return;
            }
            failureCount += 1;
            if (failureCount >= 6) {
              putTask({
                ...task,
                status: "failed",
                error_code: "import_failed",
                poll_after_ms: null,
              });
              return;
            }
            setPollFailures((current) => new Set(current).add(task.intent_id));
            const retryDelay = Math.min(30_000, 1000 * (2 ** (failureCount - 1)));
            timers.push(window.setTimeout(poll, retryDelay));
          },
        );
      };
      timers.push(window.setTimeout(poll, task.poll_after_ms));
    }
    return () => {
      stopped = true;
      for (const timer of timers) window.clearTimeout(timer);
    };
  }, [importApi, tasks]);

  function choose(file: File | undefined) {
    if (!file) return;
    const pdf =
      file.name.toLowerCase().endsWith(".pdf") &&
      (file.type === "application/pdf" || file.type === "");
    setUnsupported(!pdf);
    setSelected(pdf ? file : null);
  }

  async function confirm() {
    if (!selected || confirming.current) return;
    confirming.current = true;
    const file = selected;
    setSelected(null);
    if (input.current) input.current.value = "";
    putTask({ intent_id: `pending:${file.name}`, file_name: file.name, status: "receiving", document_id: null, error_code: null, poll_after_ms: null });
    try {
      const intent = await importApi.createImportIntent(file);
      setTasks((current) => current.filter((item) => item.intent_id !== `pending:${file.name}`));
      const receiving: ImportTask = {
        intent_id: intent.intent_id,
        file_name: file.name,
        status: "receiving",
        document_id: null,
        error_code: null,
        poll_after_ms: null,
      };
      putTask(receiving);
      acceptNewTask(receiving, await importApi.uploadImport(intent, file));
    } catch (error) {
      const code: ImportErrorCode = error instanceof ImportApiError ? error.code : "import_failed";
      setTasks((current) => current.filter((item) => item.intent_id !== `pending:${file.name}`));
      putTask({ intent_id: `failed:${file.name}`, file_name: file.name, status: "failed", document_id: null, error_code: code, poll_after_ms: null });
    } finally {
      confirming.current = false;
    }
  }

  function cancel() {
    setSelected(null);
    if (input.current) input.current.value = "";
  }

  const filteredDocuments = documents.filter((item) => {
    const needle = query.trim().toLocaleLowerCase("zh-CN");
    const matchesQuery =
      needle === "" ||
      `${item.relative_folder}/${item.filename}`.toLocaleLowerCase("zh-CN").includes(needle);
    if (!matchesQuery) return false;
    if (category === "unclassified") {
      return item.relative_folder === "" || item.authorization_source === "confirmed_import";
    }
    return true;
  });

  return (
    <section
      aria-label="文献导入区"
      className="library-panel"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        choose(event.dataTransfer.files.item(0) ?? undefined);
      }}
    >
      <div className="library-panel__header">
        <div>
          <span className="eyebrow">本地同步</span>
          <h1>文献库</h1>
        </div>
        <span className="count-badge">{documents.length}</span>
      </div>

      <label className="library-search">
        <span aria-hidden="true" className="library-search__icon">⌕</span>
        <span className="sr-only">搜索文献</span>
        <input
          aria-label="搜索文献"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索文件名或文件夹"
          type="search"
          value={query}
        />
        {query && (
          <button
            aria-label="清空搜索"
            className="library-search__clear"
            onClick={() => setQuery("")}
            type="button"
          >
            ×
          </button>
        )}
      </label>

      {/* Quick Category Views */}
      <div aria-label="文献分类" className="library-categories" role="group">
        <button
          aria-pressed={category === "all"}
          className={`library-category-item ${category === "all" ? "library-category-item--active" : ""}`}
          onClick={() => setCategory("all")}
          type="button"
        >
          <span className="library-category-item__icon">📚</span>
          <span className="library-category-item__label">全部文献</span>
          <span className="library-category-item__count">{documents.length}</span>
        </button>
        <button
          aria-pressed={category === "unclassified"}
          className={`library-category-item ${category === "unclassified" ? "library-category-item--active" : ""}`}
          onClick={() => setCategory("unclassified")}
          type="button"
        >
          <span className="library-category-item__icon">📁</span>
          <span className="library-category-item__label">未分类</span>
          <span className="library-category-item__count">
            {documents.filter((d) => d.relative_folder === "" || d.authorization_source === "confirmed_import").length}
          </span>
        </button>
      </div>

      <div className="library-panel__tree-container">
        <div className="library-panel__tree-header">
          <span>文件目录</span>
        </div>
        <DocumentTree
          activeDocumentId={activeDocumentId}
          documents={filteredDocuments}
          onOpenDocument={onOpenDocument}
        />
      </div>

      {query.trim() !== "" && filteredDocuments.length === 0 && (
        <p className="empty-copy">没有匹配的文献</p>
      )}
      {documentsFailed && <p role="alert">文献列表暂时无法读取</p>}

      {/* Import & Storage Footer */}
      <div className="library-panel__footer">
        <button
          aria-label="打开文件选择器"
          className="library-drop"
          onClick={() => input.current?.click()}
          type="button"
        >
          <div className="library-drop__icon">＋</div>
          <div className="library-drop__text">
            <strong>导入文献</strong>
            <span>点击选择或拖入 PDF</span>
          </div>
        </button>
        <input
          aria-hidden="true"
          aria-label="导入文献"
          className="sr-only"
          ref={input}
          tabIndex={-1}
          type="file"
          onChange={(event) => choose(event.target.files?.[0])}
        />

        <div className="library-storage-info">
          <div className="library-storage-info__header">
            <span>本地只读库</span>
            <span>{documents.length} 篇已同步</span>
          </div>
        </div>
      </div>

      {unsupported && <p>首版暂不导入；以后可在单次明确授权后用于云处理</p>}
      {selected && (
        <ImportConfirmDialog
          fileName={selected.name}
          status="awaiting_confirmation"
          onConfirm={() => void confirm()}
          onCancel={cancel}
        />
      )}
      {tasks.map((task) => (
        <div key={task.intent_id}>
          <ImportConfirmDialog
            fileName={task.file_name}
            status={task.status}
            errorCode={task.error_code}
            onConfirm={() => undefined}
            onCancel={() => undefined}
          />
          {pollFailures.has(task.intent_id) && <p>状态更新暂时失败，正在重试</p>}
        </div>
      ))}
    </section>
  );
}
