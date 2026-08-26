import { useState } from "react";
import type { DocumentListItem } from "../library/documents";

type DashboardHomeProps = {
  documents: DocumentListItem[];
  onOpenDocument(documentId: string): void;
  onAskQuestion?(question: string): void;
};

const DEFAULT_QUESTIONS = [
  "这篇文献的核心研究问题与主要贡献是什么？",
  "作者采用了哪些研究方法与实验设计？是否存在局限？",
  "这项研究与相关领域的经典工作有哪些承接与突破？",
  "该研究对我们当前的课题与写作有哪些具体启发？",
];

const ALTERNATIVE_QUESTIONS = [
  "论文的核心论点与理论假设是什么？",
  "实验数据集与评估指标是如何构建和选取的？",
  "作者在讨论部分指出了哪些潜在的威胁与未来方向？",
  "有哪些关键的段落或结论适合作为学术引用？",
];

export function DashboardHome({
  documents,
  onOpenDocument,
  onAskQuestion,
}: DashboardHomeProps) {
  const [questionSet, setQuestionSet] = useState<0 | 1>(0);

  const activeQuestions = questionSet === 0 ? DEFAULT_QUESTIONS : ALTERNATIVE_QUESTIONS;

  const recentDocuments = documents.slice(0, 3);
  const primaryDoc = documents[0];

  function handleStartReading() {
    if (primaryDoc) {
      onOpenDocument(primaryDoc.document_id);
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
            每天进步一点点，<br />
            距离答案更近一步。
          </h1>
          <p className="dashboard-hero__subtitle">
            从一篇文献开始，构建自己的学术知识体系。
          </p>
          <div className="dashboard-hero__actions">
            <button
              className="dashboard-hero__cta"
              onClick={handleStartReading}
              type="button"
            >
              <span>{primaryDoc ? "继续阅读" : "选择文献阅读"}</span>
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
        {/* Continue Reading */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>继续阅读</h3>
            <span className="dashboard-card__link">最近访问</span>
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
                  <div className="dashboard-reading-item__icon">PDF</div>
                  <div className="dashboard-reading-item__info">
                    <h4 title={doc.filename}>{doc.filename}</h4>
                    <p>{doc.relative_folder || "根目录"} · 本地文献</p>
                  </div>
                </button>
              ))
            ) : (
              <div className="dashboard-empty-card">
                <p>文献库暂无打开记录</p>
                <small>请从左侧文献库选择或导入 PDF</small>
              </div>
            )}
          </div>
        </section>

        {/* Library Stats */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>文献库概览</h3>
            <span className="dashboard-card__tag">实时统计</span>
          </div>
          <div className="dashboard-goal">
            <div className="dashboard-goal__stat">
              <span className="dashboard-goal__number">
                {documents.length}
              </span>
              <span className="dashboard-goal__total"> 篇文献</span>
            </div>
            <p className="dashboard-goal__hint">
              {documents.length === 0
                ? "尚未导入文献，从左侧文献库或拖拽 PDF 开始"
                : "选择一篇文献开始阅读，研究助手将为你提供问答支持"}
            </p>
          </div>
        </section>

        {/* Research Tips */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>研究助手</h3>
            <span className="dashboard-card__tag">使用提示</span>
          </div>
          <div className="dashboard-notes-list">
            <div className="dashboard-note-item">
              <span className="dashboard-note-item__bullet">💡</span>
              <div className="dashboard-note-item__content">
                <p>选中 PDF 文字可快速翻译，右侧面板可向 AI 提问</p>
              </div>
            </div>
            <div className="dashboard-note-item">
              <span className="dashboard-note-item__bullet">💡</span>
              <div className="dashboard-note-item__content">
                <p>在阅读器中可对重点内容添加高亮批注和笔记</p>
              </div>
            </div>
            <div className="dashboard-note-item">
              <span className="dashboard-note-item__bullet">💡</span>
              <div className="dashboard-note-item__content">
                <p>研究助手支持跨文献提问，自动引用原文页码</p>
              </div>
            </div>
          </div>
        </section>
      </div>

      {/* Main Grid: Row 2 */}
      <div className="dashboard-grid dashboard-grid--two">
        {/* Recommended Research Questions */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>推荐研究思考问题</h3>
            <button
              className="dashboard-card__button"
              onClick={() => setQuestionSet((prev) => (prev === 0 ? 1 : 0))}
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
                <span className="dashboard-question-pill__num">?</span>
                <span className="dashboard-question-pill__text">{q}</span>
                <span className="dashboard-question-pill__action">提问 ↗</span>
              </button>
            ))}
          </div>
        </section>

        {/* Literature Overview / Speed Read */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>文献速览</h3>
            <span className="dashboard-card__tag">库内概览 ({documents.length})</span>
          </div>
          <div className="dashboard-speedread">
            {documents.slice(0, 4).map((doc) => (
              <button
                key={doc.document_id}
                className="dashboard-speedread-item"
                onClick={() => onOpenDocument(doc.document_id)}
                type="button"
              >
                <div className="dashboard-speedread-item__badge">PDF</div>
                <div className="dashboard-speedread-item__body">
                  <h5>{doc.filename}</h5>
                  <p>{doc.relative_folder ? `/${doc.relative_folder}` : "本地文献"}</p>
                </div>
                <span className="dashboard-speedread-item__arrow">→</span>
              </button>
            ))}
            {documents.length === 0 && (
              <div className="dashboard-empty-card">
                <p>暂无导入的文献</p>
                <small>支持点击或拖拽 PDF 文件导入本地库</small>
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
