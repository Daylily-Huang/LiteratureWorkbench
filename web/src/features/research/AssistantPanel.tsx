import { useState, type FormEvent } from "react";

import type { ReaderDocument } from "../reader/PdfReader";


type Fetcher = typeof fetch;

export type ResearchCitation = {
  physical_page: number;
  excerpt: string;
  rects: number[][];
};

export type LibraryCitation = {
  document_id: string;
  display_name: string;
  physical_page: number;
  excerpt: string;
  rects: number[][];
};

type DocumentAnswer = {
  scope: "document";
  answer: string;
  citations: ResearchCitation[];
  provider: string;
  model: string;
  fallback_reason: string | null;
};

type LibraryAnswer = {
  scope: "library";
  answer: string;
  citations: LibraryCitation[];
  provider: string;
  model: string;
  fallback_reason: string | null;
  indexed_documents: number;
};

type Answer = DocumentAnswer | LibraryAnswer;

type Scope = "document" | "library";
type AssistantMode = "qa" | "summary" | "translate" | "inspiration";

type AssistantPanelProps = {
  document: ReaderDocument | null;
  onJumpToCitation(citation: ResearchCitation): void;
  onJumpToLibraryCitation?(citation: LibraryCitation): void;
  onOpenSettings?(): void;
  fetcher?: Fetcher;
  initialQuestion?: string;
};


const MAX_QUESTION_CHARS = 2000;


async function csrf(fetcher: Fetcher): Promise<string> {
  const response = await fetcher("/api/session/current", { credentials: "same-origin" });
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
}: AssistantPanelProps) {
  const [question, setQuestion] = useState(initialQuestion);
  const [scope, setScope] = useState<Scope>("document");
  const [mode, setMode] = useState<AssistantMode>("qa");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);

  // No document open: only the library scope is meaningful, so it wins.
  const effectiveScope: Scope = readerDocument === null ? "library" : scope;
  const trimmed = question.trim();
  const canAsk = trimmed.length > 0 && !busy;

  async function submit(event?: FormEvent, customQuery?: string): Promise<void> {
    if (event) event.preventDefault();
    const queryToAsk = (customQuery ?? trimmed).trim();
    if (!queryToAsk || busy) return;
    setBusy(true);
    setErrorCode(null);
    try {
      const token = await csrf(fetcher);
      const url = effectiveScope === "library"
        ? "/api/libraries/ask"
        : `/api/documents/${readerDocument?.documentId ?? ""}/ask`;
      const response = await fetcher(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-LW-CSRF": token },
        body: JSON.stringify({ question: queryToAsk }),
      });
      const value = await response.json() as (
        Omit<DocumentAnswer, "scope"> & Omit<LibraryAnswer, "scope"> & { code?: string }
      );
      if (!response.ok) {
        setAnswer(null);
        setErrorCode(value.code ?? "research_failed");
        return;
      }
      setAnswer({ ...value, scope: effectiveScope } as Answer);
      if (value.provider === "deepseek") {
        window.dispatchEvent(new Event("deepseek-usage-updated"));
      }
    } catch {
      setAnswer(null);
      setErrorCode("research_failed");
    } finally {
      setBusy(false);
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

      {/* Mode Tabs */}
      <div className="assistant-mode-tabs" role="tablist" aria-label="助手模式">
        <button
          className={`assistant-mode-tab ${mode === "qa" ? "assistant-mode-tab--active" : ""}`}
          onClick={() => setMode("qa")}
          role="tab"
          aria-selected={mode === "qa"}
          type="button"
        >
          问答
        </button>
        <button
          className={`assistant-mode-tab ${mode === "summary" ? "assistant-mode-tab--active" : ""}`}
          onClick={() => {
            setMode("summary");
            setQuestion("请用学术严谨的语言总结本文的核心研究问题、主要方法、关键实验结果与结论。");
          }}
          role="tab"
          aria-selected={mode === "summary"}
          type="button"
        >
          总结
        </button>
        <button
          className={`assistant-mode-tab ${mode === "translate" ? "assistant-mode-tab--active" : ""}`}
          onClick={() => {
            setMode("translate");
            setQuestion("请将选中的学术段落翻译为地道、准确的中文学术表达。");
          }}
          role="tab"
          aria-selected={mode === "translate"}
          type="button"
        >
          翻译
        </button>
        <button
          className={`assistant-mode-tab ${mode === "inspiration" ? "assistant-mode-tab--active" : ""}`}
          onClick={() => {
            setMode("inspiration");
            setQuestion("基于当前文献，有哪些值得深入探索的新课题或交叉方向？");
          }}
          role="tab"
          aria-selected={mode === "inspiration"}
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
          {answer.citations.length > 0 && (
            <div aria-label="引用来源" className="assistant-citations">
              <span className="assistant-citations__title">引用来源:</span>
              {answer.scope === "library"
                ? answer.citations.map((citation) => (
                  <button
                    className="assistant-citation"
                    key={`${citation.document_id}:${citation.physical_page}`}
                    onClick={() => onJumpToLibraryCitation?.(citation)}
                    title={citation.excerpt}
                    type="button"
                  >
                    《{citation.display_name}》第 {citation.physical_page} 页
                  </button>
                ))
                : answer.citations.map((citation) => (
                  <button
                    className="assistant-citation"
                    key={citation.physical_page}
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
