import { useState } from "react";
import type { ReadingRecords } from "../library/readingRecords";
import type { DocumentListItem } from "../library/documents";

type DashboardHomeProps = {
  documents: DocumentListItem[];
  readingRecords?: ReadingRecords;
  onOpenDocument(documentId: string): void;
  onAskQuestion?(question: string): void;
  onTriggerImport?(): void;
};

const DEFAULT_QUESTIONS = [
  "文献库中有哪些主要研究问题与代表性成果？",
  "比较文献库中主要研究方法的适用条件与局限。",
  "文献库中的研究有哪些相互支持或矛盾的发现？",
  "基于文献库的证据，哪些问题值得进一步研究？",
];

const ALTERNATIVE_QUESTIONS = [
  "比较文献库中不同研究的核心论点与理论假设。",
  "实验数据集与评估指标是如何构建和选取的？",
  "作者在讨论部分指出了哪些潜在的威胁与未来方向？",
  "有哪些关键的段落或结论适合作为学术引用？",
];

export function DashboardHome({
  documents,
  readingRecords = {},
  onOpenDocument,
  onAskQuestion,
  onTriggerImport,
}: DashboardHomeProps) {
  const [questionSet, setQuestionSet] = useState<0 | 1>(0);

  const activeQuestions = questionSet === 0 ? DEFAULT_QUESTIONS : ALTERNATIVE_QUESTIONS;

  const recentlyRead = documents.filter(doc => (readingRecords[doc.document_id]?.lastOpened ?? 0) > 0)
    .sort((a, b) => (readingRecords[b.document_id]?.lastOpened ?? 0) - (readingRecords[a.document_id]?.lastOpened ?? 0));
  const recentDocuments = (recentlyRead.length ? recentlyRead : documents).slice(0, 3);
  const primaryDoc = recentDocuments[0];
  const lastRead = primaryDoc ? readingRecords[primaryDoc.document_id] : undefined;

  function handleStartReading() {
    if (primaryDoc) {
      onOpenDocument(primaryDoc.document_id);
    } else if (onTriggerImport) {
      onTriggerImport();
    }
  }

  function handleQuestionClick(q: string) {
    if (onAskQuestion) {
      onAskQuestion(q);
    }
  }

  return (
    <div className="dashboard-home" role="region" aria-label="文献工作台主页">
      {/* Hero Banner */}
      <section className="dashboard-hero">
        <div className="dashboard-hero__content">
          <span className="dashboard-hero__greeting">
            <span className="dashboard-hero__sun">☀</span> 专注当下，积累复利
          </span>
          <h1 className="dashboard-hero__title">
            {recentlyRead.length ? "接着上次的思路，" : "从一篇文献开始，"}<br />
            让研究向前一步。
          </h1>
          <p className="dashboard-hero__subtitle">
            {lastRead?.lastOpened ? `${primaryDoc?.filename} · 上次读到第 ${lastRead.page} 页` : "找回原文、留下思考，把阅读变成可追溯的积累。"}
          </p>
          <div className="dashboard-hero__actions">
            <button
              className="dashboard-hero__cta"
              onClick={handleStartReading}
              type="button"
            >
              <span>{primaryDoc ? (lastRead?.lastOpened ? "继续阅读" : "开始阅读") : "导入文献开始阅读"}</span>
              <span aria-hidden="true" className="dashboard-hero__arrow">→</span>
            </button>
            <span className="dashboard-hero__badge">
              知识是远行的灯塔，也是脚下的路。
            </span>
          </div>
        </div>
        <div className="dashboard-hero__art" aria-hidden="true">
          <div className="dashboard-hero__landscape">
            <div className="dashboard-hero__sun-orb" />
            <div className="dashboard-hero__mountain dashboard-hero__mountain--back" />
            <div className="dashboard-hero__mountain dashboard-hero__mountain--mid" />
            <div className="dashboard-hero__mountain dashboard-hero__mountain--front" />
          </div>
        </div>
      </section>

      {/* Main Grid: Row 1 */}
      <div className="dashboard-grid dashboard-grid--three">
        {/* Quick Access */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>{recentlyRead.length ? "最近阅读" : "文献快捷入口"}</h3>
            <span className="dashboard-card__tag">快速打开</span>
          </div>
          <div className="dashboard-reading-list">
            {recentDocuments.length > 0 ? (
              recentDocuments.map((doc) => (
                <button
                  key={doc.document_id}
                  className="dashboard-reading-item"
                  onClick={() => onOpenDocument(doc.document_id)}
                  type="button"
                >
                  <span className="dashboard-reading-item__icon">PDF</span>
                  <div className="dashboard-reading-item__info">
                    <h4>{doc.filename}</h4>
                    <p>{readingRecords[doc.document_id]?.lastOpened ? `第 ${readingRecords[doc.document_id]?.page} / ${readingRecords[doc.document_id]?.totalPages} 页` : doc.relative_folder || "默认文件夹"}</p>
                  </div>
                  <span className="dashboard-card__link">阅读 →</span>
                </button>
              ))
            ) : (
              <div className="dashboard-empty-card">
                <p>还没有阅读记录</p>
                <small>导入 PDF 后，阅读页码会自动记住</small>
              </div>
            )}
          </div>
        </section>

        {/* Library Overview */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>文献库概览</h3>
            <span className="dashboard-card__tag">真实统计</span>
          </div>
          <div className="dashboard-goal">
            <div className="dashboard-goal__stat">
              <span className="dashboard-goal__number">{documents.length}</span>
              <span className="dashboard-goal__divider">/</span>
              <span className="dashboard-goal__total">篇已收录文献</span>
            </div>
            <p className="dashboard-goal__hint">
              {documents.length > 0
                ? `${recentlyRead.length} 篇有阅读记录 · ${documents.filter(doc => readingRecords[doc.document_id]?.favorite).length} 篇已收藏`
                : "文献库当前为空，点击下方按钮或拖入 PDF 开始阅读"}
            </p>
            {onTriggerImport && (
              <button
                className="dashboard-card__button"
                onClick={onTriggerImport}
                type="button"
                style={{ alignSelf: "flex-start", marginTop: "auto" }}
              >
                + 导入新文献
              </button>
            )}
          </div>
        </section>

        {/* Academic Workflow Guidance */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>科研工作流</h3>
            <span className="dashboard-card__tag">功能指引</span>
          </div>
          <div className="dashboard-notes-list">
            <div className="dashboard-note-item">
              <span className="dashboard-note-item__bullet">•</span>
              <div className="dashboard-note-item__content">
                <p><strong>PDF 精读与划选翻译</strong>：选中文字即可翻译、标注和记录思考</p>
                <time>深度阅读模式</time>
              </div>
            </div>
            <div className="dashboard-note-item">
              <span className="dashboard-note-item__bullet">•</span>
              <div className="dashboard-note-item__content">
                <p><strong>证据溯源与问答</strong>：按单篇或全库提问，点击来源返回原文核对</p>
                <time>可信科研助手</time>
              </div>
            </div>
          </div>
        </section>
      </div>

      {/* Main Grid: Row 2 */}
      <div className="dashboard-grid dashboard-grid--two">
        {/* Recommended Research Prompts */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>推荐研究思考问题</h3>
            <button
              className="dashboard-card__button"
              onClick={() => setQuestionSet((val) => (val === 0 ? 1 : 0))}
              type="button"
            >
              换一批
            </button>
          </div>
          <div className="dashboard-questions">
            {activeQuestions.map((q, idx) => (
              <button
                key={idx}
                className="dashboard-question-pill"
                onClick={() => handleQuestionClick(q)}
                type="button"
              >
                <span className="dashboard-question-pill__num">{idx + 1}</span>
                <span className="dashboard-question-pill__text">{q}</span>
                <span className="dashboard-question-pill__action">探讨 →</span>
              </button>
            ))}
          </div>
        </section>

        {/* Quick Literature List */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>文献速览</h3>
            <span className="dashboard-card__tag">共 {documents.length} 篇</span>
          </div>
          <div className="dashboard-speedread">
            {documents.slice(0, 4).map((doc) => (
              <button
                key={doc.document_id}
                className="dashboard-speedread-item"
                onClick={() => onOpenDocument(doc.document_id)}
                type="button"
              >
                <span className="dashboard-speedread-item__badge">PDF</span>
                <div className="dashboard-speedread-item__body">
                  <h5>{doc.filename}</h5>
                  <p>{doc.relative_folder || "未分类"}</p>
                </div>
                <span className="dashboard-speedread-item__arrow">›</span>
              </button>
            ))}
            {documents.length === 0 && (
              <div className="dashboard-empty-card">
                <p>文献库暂无文档</p>
                <small>拖拽 PDF 到左侧或点击导入开始建立文献库</small>
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
