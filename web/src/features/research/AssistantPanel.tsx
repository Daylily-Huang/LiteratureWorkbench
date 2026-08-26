import { useEffect, useRef, useState, type FormEvent } from "react";

import { isValidPdfRect, type PdfRect } from "../reader/CitationHighlight";
import type { ReaderDocument } from "../reader/PdfReader";


type Fetcher = typeof fetch;

export type ResearchCitation = {
  physical_page: number;
  excerpt: string;
  rects: readonly PdfRect[];
};

export type LibraryCitation = {
  document_id: string;
  display_name: string;
  physical_page: number;
  excerpt: string;
  rects: readonly PdfRect[];
};

export type DocumentAnswer = {
  scope: "document";
  answer: string;
  citations: ResearchCitation[];
  provider: string;
  model: string;
  fallback_reason: string | null;
  citation_status?: "verified" | "missing";
};

export type LibraryAnswer = {
  scope: "library";
  answer: string;
  citations: LibraryCitation[];
  provider: string;
  model: string;
  fallback_reason: string | null;
  indexed_documents: number;
  citation_status?: "verified" | "missing";
};

export type Answer = DocumentAnswer | LibraryAnswer;

type Scope = "document" | "library";
type AssistantMode = "qa" | "summary" | "inspiration";

type AssistantPanelProps = {
  document: ReaderDocument | null;
  onJumpToCitation(citation: ResearchCitation): void;
  onJumpToLibraryCitation?(citation: LibraryCitation): void;
  onOpenSettings?(): void;
  fetcher?: Fetcher;
  initialQuestion?: string;
  draftQuestion?: string;
  onDraftQuestionConsumed?(): void;
};


const MAX_QUESTION_CHARS = 2000;


async function csrf(fetcher: Fetcher, signal?: AbortSignal): Promise<string> {
  const response = await fetcher("/api/session/current", {
    credentials: "same-origin",
    signal,
  });
  if (!response.ok) throw new Error("session_unavailable");
  const value = await response.json() as { csrf_token: string };
  return value.csrf_token;
}


export function assistantErrorMessage(code: string | null | undefined): string {
  const known: Record<string, string> = {
    document_not_found: "文献信息不可用，请重新打开文献",
    parse_required: "文档尚未完成解析，请稍后再提问",
    question_empty: "问题不能为空",
    question_too_long: `问题请控制在 ${MAX_QUESTION_CHARS} 字以内`,
    deepseek_key_unavailable: "DeepSeek 密钥未配置，可在设置中添加",
    credential_store_failed: "DeepSeek 凭据暂时无法读取",
    deepseek_authorization_failed: "DeepSeek 密钥未获授权",
    deepseek_request_rejected: "DeepSeek 配置或请求被拒绝",
    qwen_failed: "本地模型暂时不可用",
  };
  return known[code ?? ""] ?? "回答失败，请重试";
}


function parseResearchCitation(item: unknown): ResearchCitation | null {
  if (!item || typeof item !== "object") return null;
  const obj = item as Record<string, unknown>;
  if (typeof obj.physical_page !== "number" || !Number.isInteger(obj.physical_page) || obj.physical_page < 1) {
    return null;
  }
  if (typeof obj.excerpt !== "string") return null;
  const rawRects = Array.isArray(obj.rects) ? obj.rects : [];
  const rects: PdfRect[] = [];
  for (const r of rawRects) {
    if (isValidPdfRect(r)) rects.push(r);
  }
  return {
    physical_page: obj.physical_page,
    excerpt: obj.excerpt,
    rects,
  };
}


function parseLibraryCitation(item: unknown): LibraryCitation | null {
  if (!item || typeof item !== "object") return null;
  const obj = item as Record<string, unknown>;
  if (typeof obj.document_id !== "string" || !obj.document_id) return null;
  if (typeof obj.display_name !== "string") return null;
  if (typeof obj.physical_page !== "number" || !Number.isInteger(obj.physical_page) || obj.physical_page < 1) {
    return null;
  }
  if (typeof obj.excerpt !== "string") return null;
  const rawRects = Array.isArray(obj.rects) ? obj.rects : [];
  const rects: PdfRect[] = [];
  for (const r of rawRects) {
    if (isValidPdfRect(r)) rects.push(r);
  }
  return {
    document_id: obj.document_id,
    display_name: obj.display_name,
    physical_page: obj.physical_page,
    excerpt: obj.excerpt,
    rects,
  };
}


function parseDocumentResponse(data: unknown): DocumentAnswer | null {
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  if (typeof obj.answer !== "string") return null;
  if (typeof obj.provider !== "string") return null;
  if (typeof obj.model !== "string") return null;
  const fallback_reason = typeof obj.fallback_reason === "string" ? obj.fallback_reason : null;
  const rawCitations = Array.isArray(obj.citations) ? obj.citations : [];
  const citations: ResearchCitation[] = [];
  for (const c of rawCitations) {
    const parsed = parseResearchCitation(c);
    if (parsed) citations.push(parsed);
  }
  const citation_status = obj.citation_status === "missing" || obj.citation_status === "verified"
    ? obj.citation_status
    : undefined;
  return {
    scope: "document",
    answer: obj.answer,
    citations,
    provider: obj.provider,
    model: obj.model,
    fallback_reason,
    citation_status,
  };
}


function parseLibraryResponse(data: unknown): LibraryAnswer | null {
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  if (typeof obj.answer !== "string") return null;
  if (typeof obj.provider !== "string") return null;
  if (typeof obj.model !== "string") return null;
  const fallback_reason = typeof obj.fallback_reason === "string" ? obj.fallback_reason : null;
  const indexed_documents = typeof obj.indexed_documents === "number" ? obj.indexed_documents : 0;
  const rawCitations = Array.isArray(obj.citations) ? obj.citations : [];
  const citations: LibraryCitation[] = [];
  for (const c of rawCitations) {
    const parsed = parseLibraryCitation(c);
    if (parsed) citations.push(parsed);
  }
  const citation_status = obj.citation_status === "missing" || obj.citation_status === "verified"
    ? obj.citation_status
    : undefined;
  return {
    scope: "library",
    answer: obj.answer,
    citations,
    provider: obj.provider,
    model: obj.model,
    fallback_reason,
    indexed_documents,
    citation_status,
  };
}


function providerBadge(answer: Answer): string {
  if (answer.provider === "deepseek" && answer.model) return `DeepSeek · ${answer.model}`;
  if (answer.provider === "qwen") {
    return answer.fallback_reason !== null
      ? "本地模型回答（云端不可用，已自动切换）"
      : "本地模型回答";
  }
  return answer.scope === "library" ? "库内检索" : "文档内检索";
}

const QUICK_PROMPTS = [
  "总结本文的核心贡献与创新点",
  "提炼研究方法与实验设计",
  "分析本文存在的局限性与潜在威胁",
  "生成结构化的论文写作大纲",
];

export function AssistantPanel({
  document: readerDocument,
  onJumpToCitation,
  onJumpToLibraryCitation,
  onOpenSettings,
  fetcher = fetch,
  initialQuestion = "",
  draftQuestion,
  onDraftQuestionConsumed,
}: AssistantPanelProps) {
  const [question, setQuestion] = useState(initialQuestion);
  const [scope, setScope] = useState<Scope>("document");
  const [mode, setMode] = useState<AssistantMode>("qa");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);

  const requestIdRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const prevDocumentIdRef = useRef<string | null>(readerDocument?.documentId ?? null);

  // Sync draft question from parent (e.g. Dashboard)
  useEffect(() => {
    if (draftQuestion && draftQuestion.trim()) {
      setQuestion(draftQuestion);
      onDraftQuestionConsumed?.();
    }
  }, [draftQuestion, onDraftQuestionConsumed]);

  // Cancel in-flight requests and clear state on document switch
  useEffect(() => {
    const currentDocId = readerDocument?.documentId ?? null;
    if (prevDocumentIdRef.current !== currentDocId) {
      prevDocumentIdRef.current = currentDocId;
      abortRef.current?.abort();
      abortRef.current = null;
      requestIdRef.current++;
      setBusy(false);
      setAnswer(null);
      setErrorCode(null);
    }
  }, [readerDocument?.documentId]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  // No document open: only the library scope is meaningful, so it wins.
  const effectiveScope: Scope = readerDocument === null ? "library" : scope;
  const trimmed = question.trim();
  const canAsk = trimmed.length > 0 && !busy;

  async function submit(event?: FormEvent, customQuery?: string): Promise<void> {
    if (event) event.preventDefault();
    const queryToAsk = (customQuery ?? trimmed).trim();
    if (!queryToAsk || busy) return;

    const requestId = ++requestIdRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setBusy(true);
    setErrorCode(null);

    try {
      const token = await csrf(fetcher, controller.signal);
      if (requestId !== requestIdRef.current) return;

      const url = effectiveScope === "library"
        ? "/api/libraries/ask"
        : `/api/documents/${readerDocument?.documentId ?? ""}/ask`;

      const response = await fetcher(url, {
        method: "POST",
        credentials: "same-origin",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-LW-CSRF": token },
        body: JSON.stringify({ question: queryToAsk }),
      });

      if (requestId !== requestIdRef.current) return;

      const rawJson: unknown = await response.json();
      if (requestId !== requestIdRef.current) return;

      if (!response.ok) {
        setAnswer(null);
        const code = rawJson && typeof rawJson === "object" && "code" in rawJson && typeof (rawJson as { code: unknown }).code === "string"
          ? (rawJson as { code: string }).code
          : "research_failed";
        setErrorCode(code);
        return;
      }

      const parsedAnswer = effectiveScope === "library"
        ? parseLibraryResponse(rawJson)
        : parseDocumentResponse(rawJson);

      if (!parsedAnswer) {
        setAnswer(null);
        setErrorCode("research_failed");
        return;
      }

      setAnswer(parsedAnswer);
      if (parsedAnswer.provider === "deepseek") {
        window.dispatchEvent(new Event("deepseek-usage-updated"));
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === "AbortError") {
        return;
      }
      if (requestId === requestIdRef.current) {
        setAnswer(null);
        setErrorCode("research_failed");
      }
    } finally {
      if (requestId === requestIdRef.current) {
        setBusy(false);
      }
    }
  }

  function handlePromptClick(prompt: string) {
    setQuestion(prompt);
  }

  const placeholder = effectiveScope === "library"
    ? "就整个文献库提问，答案会标注出自哪篇文献的哪一页"
    : "就当前文献提问，答案会标注来源页码";

  return (
    <section aria-label="研究助手问答" className="assistant-panel">
      <div className="assistant-heading">
        <div className="assistant-heading__title">
          <span className="eyebrow">AI 科研助手</span>
          <h2>研究助手</h2>
        </div>
        {onOpenSettings && (
          <button className="button button--ghost assistant-heading__settings" onClick={onOpenSettings} type="button">
            ⚙ 模型设置
          </button>
        )}
      </div>

      {/* Mode Group */}
      <div aria-label="助手模式" className="assistant-mode-tabs" role="group">
        <button
          aria-pressed={mode === "qa"}
          className={`assistant-mode-tab ${mode === "qa" ? "assistant-mode-tab--active" : ""}`}
          onClick={() => setMode("qa")}
          type="button"
        >
          问答
        </button>
        <button
          aria-pressed={mode === "summary"}
          className={`assistant-mode-tab ${mode === "summary" ? "assistant-mode-tab--active" : ""}`}
          onClick={() => {
            setMode("summary");
            setQuestion("请用学术严谨的语言总结本文的核心研究问题、主要方法、关键实验结果与结论。");
          }}
          type="button"
        >
          总结
        </button>
        <button
          aria-pressed={mode === "inspiration"}
          className={`assistant-mode-tab ${mode === "inspiration" ? "assistant-mode-tab--active" : ""}`}
          onClick={() => {
            setMode("inspiration");
            setQuestion("基于当前文献，有哪些值得深入探索的新课题或交叉方向？");
          }}
          type="button"
        >
          灵感
        </button>
      </div>

      {/* Scope Switcher */}
      <div aria-label="回答范围" className="assistant-scope" role="group">
        <button
          aria-pressed={effectiveScope === "document"}
          className={`assistant-scope__option${effectiveScope === "document" ? " assistant-scope__option--active" : ""}`}
          disabled={readerDocument === null}
          onClick={() => setScope("document")}
          title={readerDocument === null ? "打开一篇文献后可就单篇提问" : "仅基于当前文献回答"}
          type="button"
        >
          当前文献
        </button>
        <button
          aria-pressed={effectiveScope === "library"}
          className={`assistant-scope__option${effectiveScope === "library" ? " assistant-scope__option--active" : ""}`}
          onClick={() => setScope("library")}
          title="跨整个文献库检索回答"
          type="button"
        >
          整个文献库
        </button>
      </div>

      {/* Recommended Quick Prompts */}
      <div className="assistant-quick-prompts">
        <div className="assistant-quick-prompts__header">
          <span>推荐研究问题</span>
        </div>
        <div className="assistant-quick-prompts__list">
          {QUICK_PROMPTS.map((p, idx) => (
            <button
              key={idx}
              className="assistant-quick-prompt-pill"
              onClick={() => handlePromptClick(p)}
              type="button"
            >
              <span>{p}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Question Form */}
      <form className="assistant-panel__form" onSubmit={(event) => { void submit(event); }}>
        <div className="assistant-input-wrap">
          <textarea
            aria-label={effectiveScope === "library" ? "向文献库提问" : "向当前文献提问"}
            disabled={busy}
            maxLength={MAX_QUESTION_CHARS}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder={placeholder}
            value={question}
          />
        </div>
        <div className="assistant-panel__actions">
          <span className="assistant-panel__hint">
            {effectiveScope === "library" ? "基于整个文献库检索回答" : "仅基于本文档内容回答"}
          </span>
          <button
            className="button button--primary assistant-submit-btn"
            disabled={!canAsk}
            type="submit"
          >
            {busy ? "回答中…" : "提问"}
          </button>
        </div>
      </form>

      {busy && (
        <div className="assistant-status-card" role="status">
          <span className="assistant-status-card__spinner" />
          <span>正在检索相关片段并生成严谨解答…</span>
        </div>
      )}

      {errorCode !== null && (
        <p className="assistant-error" role="alert">{assistantErrorMessage(errorCode)}</p>
      )}

      {answer !== null && (
        <article className="assistant-answer">
          <div className="assistant-answer__header">
            <span className="assistant-answer__badge">{providerBadge(answer)}</span>
            {answer.scope === "library" && (
              <span className="assistant-answer__coverage">
                已检索 {answer.indexed_documents} 篇文献
              </span>
            )}
          </div>
          <p className="assistant-answer__text">{answer.answer}</p>

          {answer.citation_status === "missing" && (
            <p className="assistant-answer__missing-hint" role="note">
              回答未生成可验证的原文引用，请核对原文。
            </p>
          )}

          {answer.citations.length > 0 && (
            <div aria-label="引用来源" className="assistant-citations">
              <span className="assistant-citations__title">引用来源:</span>
              {answer.scope === "library"
                ? answer.citations.map((citation) => (
                  <button
                    className="assistant-citation"
                    key={`${citation.document_id}:${citation.physical_page}:${citation.excerpt.slice(0, 20)}`}
                    onClick={() => onJumpToLibraryCitation?.(citation)}
                    title={citation.excerpt}
                    type="button"
                  >
                    《{citation.display_name}》第 {citation.physical_page} 页
                  </button>
                ))
                : answer.citations.map((citation, cIdx) => (
                  <button
                    className="assistant-citation"
                    key={`${citation.physical_page}:${cIdx}`}
                    onClick={() => onJumpToCitation(citation)}
                    title={citation.excerpt}
                    type="button"
                  >
                    第 {citation.physical_page} 页
                  </button>
                ))}
            </div>
          )}
        </article>
      )}
    </section>
  );
}
