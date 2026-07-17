import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

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

type Point = { x: number; y: number };
type Size = { width: number; height: number };

const VIEWPORT_MARGIN = 12;
const ANCHOR_GAP = 10;
const DEFAULT_CARD_WIDTH = 360;
const DEFAULT_CARD_HEIGHT = 200;


function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
}


export function calculatePopoverPosition(
  anchor: Point,
  measured: Size,
  viewport: Size,
): { left: number; top: number } {
  const availableWidth = Math.max(0, viewport.width - VIEWPORT_MARGIN * 2);
  const availableHeight = Math.max(0, viewport.height - VIEWPORT_MARGIN * 2);
  const width = Math.min(measured.width > 0 ? measured.width : DEFAULT_CARD_WIDTH, availableWidth);
  const height = Math.min(measured.height > 0 ? measured.height : DEFAULT_CARD_HEIGHT, availableHeight);
  const rightSide = anchor.x + ANCHOR_GAP;
  const preferredLeft = rightSide + width <= viewport.width - VIEWPORT_MARGIN
    ? rightSide
    : anchor.x - ANCHOR_GAP - width;
  return {
    left: clamp(preferredLeft, VIEWPORT_MARGIN, viewport.width - VIEWPORT_MARGIN - width),
    top: clamp(anchor.y - 24, VIEWPORT_MARGIN, viewport.height - VIEWPORT_MARGIN - height),
  };
}


function clampPopoverPosition(position: Point, measured: Size, viewport: Size): Point {
  const width = Math.min(
    measured.width > 0 ? measured.width : DEFAULT_CARD_WIDTH,
    Math.max(0, viewport.width - VIEWPORT_MARGIN * 2),
  );
  const height = Math.min(
    measured.height > 0 ? measured.height : DEFAULT_CARD_HEIGHT,
    Math.max(0, viewport.height - VIEWPORT_MARGIN * 2),
  );
  return {
    x: clamp(position.x, VIEWPORT_MARGIN, viewport.width - VIEWPORT_MARGIN - width),
    y: clamp(position.y, VIEWPORT_MARGIN, viewport.height - VIEWPORT_MARGIN - height),
  };
}


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
  const notifiedTaskRef = useRef<string | null>(null);
  const cardRef = useRef<HTMLElement>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const selectionKey = JSON.stringify([
    selected.documentId,
    selected.pdfPage,
    selected.text,
    selected.pdfQuads,
  ]);
  const positionedSelectionRef = useRef(selectionKey);
  const userPositionedRef = useRef(false);
  const dragRef = useRef<{
    pointerId: number;
    offsetX: number;
    offsetY: number;
  } | null>(null);
  const [position, setPosition] = useState(() => calculatePopoverPosition(
    selected.viewportAnchor,
    { width: DEFAULT_CARD_WIDTH, height: DEFAULT_CARD_HEIGHT },
    { width: window.innerWidth, height: window.innerHeight },
  ));

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
      const current = selectedRef.current;
      const response = await fetcher("/api/translations", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-LW-CSRF": token },
        body: JSON.stringify({
          client_task_id: clientTaskId(),
          document_id: current.documentId,
          document_version_id: current.documentVersionId,
          content_id: current.contentId,
          physical_page: current.pdfPage,
          selected_text: current.text,
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
  }, [attempt, fetcher, pollInterval, selectionKey]);

  useLayoutEffect(() => {
    if (positionedSelectionRef.current !== selectionKey) {
      positionedSelectionRef.current = selectionKey;
      userPositionedRef.current = false;
    }
    function reposition() {
      const bounds = cardRef.current?.getBoundingClientRect();
      const measured = {
        width: bounds?.width ?? DEFAULT_CARD_WIDTH,
        height: bounds?.height ?? DEFAULT_CARD_HEIGHT,
      };
      const viewport = { width: window.innerWidth, height: window.innerHeight };
      setPosition((current) => {
        if (!userPositionedRef.current) {
          return calculatePopoverPosition(selected.viewportAnchor, measured, viewport);
        }
        const clamped = clampPopoverPosition(
          { x: current.left, y: current.top },
          measured,
          viewport,
        );
        return { left: clamped.x, top: clamped.y };
      });
    }
    reposition();
    window.addEventListener("resize", reposition);
    return () => window.removeEventListener("resize", reposition);
  }, [copied, requestFailed, selected.viewportAnchor, selectionKey, task]);

  useEffect(() => {
    if (
      task?.status === "completed"
      && task.provider === "deepseek"
      && task.id !== notifiedTaskRef.current
    ) {
      notifiedTaskRef.current = task.id;
      window.dispatchEvent(new Event("deepseek-usage-updated"));
    }
  }, [task]);

  function startDrag(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.target instanceof Element && event.target.closest("button") !== null) return;
    const bounds = cardRef.current?.getBoundingClientRect();
    if (bounds === undefined) return;
    dragRef.current = {
      pointerId: event.pointerId,
      offsetX: event.clientX - bounds.left,
      offsetY: event.clientY - bounds.top,
    };
    userPositionedRef.current = true;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  }

  function moveDrag(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    const bounds = cardRef.current?.getBoundingClientRect();
    if (drag === null || drag.pointerId !== event.pointerId || bounds === undefined) return;
    const clamped = clampPopoverPosition(
      { x: event.clientX - drag.offsetX, y: event.clientY - drag.offsetY },
      { width: bounds.width, height: bounds.height },
      { width: window.innerWidth, height: window.innerHeight },
    );
    setPosition({ left: clamped.x, top: clamped.y });
    event.preventDefault();
  }

  function finishDrag(event: ReactPointerEvent<HTMLDivElement>) {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  const failed = requestFailed || task?.status === "failed";
  const badge = task === null ? null : sourceBadge(task);

  return (
    <section
      aria-label="选区翻译"
      className="translation-popover"
      ref={cardRef}
      style={{ left: `${position.left}px`, top: `${position.top}px` }}
    >
      <div
        className="translation-popover__header"
        onPointerCancel={finishDrag}
        onPointerDown={startDrag}
        onPointerMove={moveDrag}
        onPointerUp={finishDrag}
        title="拖动翻译框"
      >
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
