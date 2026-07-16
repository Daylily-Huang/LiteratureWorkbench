import { useEffect, useRef, useState } from "react";

import { ImportConfirmDialog } from "./ImportConfirmDialog";
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
  importApi?: ImportApi;
  onOpenDocument?: (documentId: string) => void;
};

export function LibraryPanel({
  activeDocumentId = null,
  documentApi = browserDocumentApi,
  importApi = browserImportApi,
  onOpenDocument = () => undefined,
}: Props) {
  const [selected, setSelected] = useState<File | null>(null);
  const [unsupported, setUnsupported] = useState(false);
  const [tasks, setTasks] = useState<ImportTask[]>([]);
  const [documents, setDocuments] = useState<DocumentListItem[]>([]);
  const [documentsFailed, setDocumentsFailed] = useState(false);
  const [pollFailures, setPollFailures] = useState<Set<string>>(new Set());
  const input = useRef<HTMLInputElement>(null);
  const confirming = useRef(false);

  function putTask(task: ImportTask) {
    setTasks((current) => [task, ...current.filter((item) => item.intent_id !== task.intent_id)]);
  }

  async function refreshDocuments() {
    try {
      setDocuments(await documentApi.listDocuments());
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
    void documentApi.listDocuments().then(
      (items) => {
        if (!stopped) {
          setDocuments(items);
          setDocumentsFailed(false);
        }
      },
      () => {
        if (!stopped) setDocumentsFailed(true);
      },
    );
    return () => { stopped = true; };
  }, [documentApi]);

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
      const poll = () => {
        if (stopped) return;
        void importApi.getImport(task.intent_id).then(
          (next) => {
            if (stopped) return;
            setPollFailures((current) => {
              const copy = new Set(current);
              copy.delete(task.intent_id);
              return copy;
            });
            acceptNewTask(task, next);
          },
          () => {
            if (stopped) return;
            setPollFailures((current) => new Set(current).add(task.intent_id));
            timers.push(window.setTimeout(poll, 1000));
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

  return (
    <section
      aria-label="文献导入区"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        choose(event.dataTransfer.files.item(0) ?? undefined);
      }}
    >
      <h1>文献库</h1>
      <nav aria-label="文献列表" className="document-list">
        {documents.map((document) => (
          <button
            aria-current={activeDocumentId === document.document_id ? "page" : undefined}
            className="document-list__item"
            key={document.document_id}
            onClick={() => onOpenDocument(document.document_id)}
            title={document.filename}
            type="button"
          >
            {document.filename}
          </button>
        ))}
      </nav>
      {documentsFailed && <p role="alert">文献列表暂时无法读取</p>}
      <label>导入文献<input ref={input} type="file" onChange={(event) => choose(event.target.files?.[0])} /></label>
      {unsupported && <p>首版暂不导入；以后可在单次明确授权后用于云处理</p>}
      {selected && <ImportConfirmDialog fileName={selected.name} status="awaiting_confirmation" onConfirm={() => void confirm()} onCancel={cancel} />}
      {tasks.map((task) => (
        <div key={task.intent_id}>
          <ImportConfirmDialog fileName={task.file_name} status={task.status} errorCode={task.error_code} onConfirm={() => undefined} onCancel={() => undefined} />
          {pollFailures.has(task.intent_id) && <p>状态更新暂时失败，正在重试</p>}
        </div>
      ))}
    </section>
  );
}
