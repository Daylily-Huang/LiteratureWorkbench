import { useEffect, useState } from "react";

import type { SelectedText } from "../reader/selection";


type Fetcher = typeof fetch;
type TaskStatus = "queued" | "running" | "completed" | "failed";

type TranslationTask = {
  id: string;
  status: TaskStatus;
  selected_text?: string;
  translated_text?: string | null;
  source_language?: string | null;
  provider?: string | null;
  model?: string | null;
  fallback_reason?: string | null;
  error_code?: string | null;
};

type TranslationPopoverProps = {
  selected: SelectedText;
  onClose?(): void;
  fetcher?: Fetcher;
  pollInterval?: number;
};


function clientTaskId(): string {
  return globalThis.crypto.randomUUID();
}


async function csrf(fetcher: Fetcher): Promise<string> {
  const response = await fetcher("/api/session/current", { credentials: "same-origin" });
  if (!response.ok) throw new Error("session_unavailable");
  const value = await response.json() as { csrf_token: string };
  return value.csrf_token;
}


function errorMessage(code: string | null | undefined): string {
  const known: Record<string, string> = {
    deepseek_key_unavailable: "DeepSeek 密钥未配置",
    credential_store_failed: "DeepSeek 凭据暂时无法读取",
    deepseek_authorization_failed: "DeepSeek 密钥未获授权",
    deepseek_request_rejected: "DeepSeek 配置或请求被拒绝",
    selection_not_current: "选区已过期，请重新选择",
    qwen_failed: "本地备用模型暂时不可用",
  };
  return known[code ?? ""] ?? "翻译失败，请重试";
}


function sourceBadge(task: TranslationTask): string | null {
  if (task.provider === "deepseek" && task.model) return `DeepSeek · ${task.model}`;
  if (task.provider === "qwen") return "本地备用模型 · literature-workbench-qwen";
  return null;
}


export function TranslationPopover({
  selected,
  onClose,
  fetcher = fetch,
  pollInterval = 500,
}: TranslationPopoverProps) {
  const [attempt, setAttempt] = useState(0);
  const [task, setTask] = useState<TranslationTask | null>(null);
  const [requestFailed, setRequestFailed] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function readTask(taskId: string): Promise<void> {
      const response = await fetcher(`/api/translations/${taskId}`, {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("translation_poll_failed");
      const next = await response.json() as TranslationTask;
      if (!active) return;
      setTask(next);
      if (next.status === "queued" || next.status === "running") {
        timer = setTimeout(() => { void readTask(taskId).catch(fail); }, pollInterval);
      }
    }

    function fail(): void {
      if (active) setRequestFailed(true);
    }

    async function create(): Promise<void> {
      setTask(null);
      setRequestFailed(false);
      setCopied(false);
      const token = await csrf(fetcher);
      const response = await fetcher("/api/translations", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-LW-CSRF": token },
        body: JSON.stringify({
          client_task_id: clientTaskId(),
          document_id: selected.documentId,
          document_version_id: selected.documentVersionId,
          content_id: selected.contentId,
          physical_page: selected.pdfPage,
          selected_text: selected.text,
          target_language: "zh-CN",
        }),
      });
      const value = await response.json() as TranslationTask & { code?: string };
      if (!response.ok) {
        if (active) setTask({ id: "", status: "failed", error_code: value.code });
        return;
      }
      if (!active) return;
      setTask(value);
      if (value.status === "queued" || value.status === "running") {
        timer = setTimeout(() => { void readTask(value.id).catch(fail); }, pollInterval);
      }
    }

    void create().catch(fail);
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [attempt, fetcher, pollInterval, selected]);

  const failed = requestFailed || task?.status === "failed";
  const badge = task === null ? null : sourceBadge(task);

  return (
    <section aria-label="选区翻译" className="translation-popover">
      <div className="translation-popover__header">
        <strong>选区翻译</strong>
        {onClose && <button aria-label="关闭翻译" onClick={onClose} type="button">×</button>}
      </div>
      <p className="translation-popover__source">{selected.text}</p>
      {(task === null || task.status === "queued" || task.status === "running") && !requestFailed && (
        <p role="status">正在翻译…</p>
      )}
      {task?.status === "completed" && (
        <>
          <p className="translation-popover__result">{task.translated_text}</p>
          {badge && <p className="translation-popover__badge">{badge}</p>}
          <button
            aria-label="复制译文"
            onClick={() => {
              void navigator.clipboard.writeText(task.translated_text ?? "").then(() => setCopied(true));
            }}
            type="button"
          >
            {copied ? "已复制" : "复制"}
          </button>
        </>
      )}
      {failed && (
        <>
          <p role="alert">{errorMessage(task?.error_code)}</p>
          <button aria-label="重试翻译" onClick={() => setAttempt((value) => value + 1)} type="button">
            重试
          </button>
        </>
      )}
    </section>
  );
}
