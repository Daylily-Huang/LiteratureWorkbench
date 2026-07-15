import { useEffect, useRef, useState } from "react";

import { ImportConfirmDialog } from "./ImportConfirmDialog";
import { browserImportApi, type ImportApi, type ImportTask } from "./dropFiles";

type Props = { importApi?: ImportApi };

export function LibraryPanel({ importApi = browserImportApi }: Props) {
  const [selected, setSelected] = useState<File | null>(null);
  const [unsupported, setUnsupported] = useState(false);
  const [task, setTask] = useState<ImportTask | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let stopped = false;
    void importApi.listActiveImports().then((tasks) => {
      const first = tasks.at(0);
      if (!stopped && first) setTask(first);
    });
    return () => { stopped = true; };
  }, [importApi]);

  useEffect(() => {
    if (!task || task.poll_after_ms === null) return;
    const timer = window.setTimeout(() => {
      void importApi.getImport(task.intent_id).then(setTask);
    }, task.poll_after_ms);
    return () => window.clearTimeout(timer);
  }, [importApi, task]);

  function choose(file: File | undefined) {
    if (!file) return;
    const pdf =
      file.name.toLowerCase().endsWith(".pdf") &&
      (file.type === "application/pdf" || file.type === "");
    setUnsupported(!pdf);
    setSelected(pdf ? file : null);
    setTask(null);
  }

  async function confirm() {
    if (!selected) return;
    const file = selected;
    setTask({ intent_id: "pending", status: "receiving", document_id: null, error_code: null, poll_after_ms: null });
    try {
      const intent = await importApi.createImportIntent(file);
      setTask({ intent_id: intent.intent_id, status: "receiving", document_id: null, error_code: null, poll_after_ms: null });
      setTask(await importApi.uploadImport(intent, file));
    } catch {
      setTask({ intent_id: "failed", status: "failed", document_id: null, error_code: "import_failed", poll_after_ms: null });
    } finally {
      setSelected(null);
      if (input.current) input.current.value = "";
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
      <label>导入文献<input ref={input} type="file" onChange={(event) => choose(event.target.files?.[0])} /></label>
      {unsupported && <p>首版暂不导入；以后可在单次明确授权后用于云处理</p>}
      {selected && <ImportConfirmDialog fileName={selected.name} status="awaiting_confirmation" onConfirm={() => void confirm()} onCancel={cancel} />}
      {task && <ImportConfirmDialog fileName={selected?.name ?? "PDF"} status={task.status} errorCode={task.error_code} onConfirm={() => undefined} onCancel={() => undefined} />}
    </section>
  );
}
