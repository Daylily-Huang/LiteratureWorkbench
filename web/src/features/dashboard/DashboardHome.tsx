import { useState } from "react";
import type { DocumentListItem } from "../library/documents";

type DashboardHomeProps = {
  documents: DocumentListItem[];
  onOpenDocument(documentId: string): void;
  onAskQuestion?(question: string): void;
  onTriggerImport?(): void;
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
  onTriggerImport,
}: DashboardHomeProps) {
  const [questionSet, setQuestionSet] = useState<0 | 1>(0);

  const activeQuestions = questionSet === 0 ? DEFAULT_QUESTIONS : ALTERNATIVE_QUESTIONS;

  const recentDocuments = documents.slice(0, 3);
  const primaryDoc = documents[0];

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
              <span>{primaryDoc ? "继续阅读" : "导入文献开始阅读"}</span>
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
                  <span className="dashboard-reading-item__icon">PDF</span>
                  <div className="dashboard-reading-item__info">
                    <h4>{doc.filename}</h4>
                    <p>{doc.relative_folder || "未分类"}</p>
                    <div className="dashboard-progress">
                      <div className="dashboard-progress__bar" style={{ width: "65%" }} />
                    </div>
                  </div>
                  <span className="dashboard-reading-item__percent">65%</span>
                </button>
              ))
            ) : (
              <div className="dashboard-empty-card">
                <p>暂无正在阅读的文献</p>
                <small>导入文献后将在此处追踪阅读进度</small>
              </div>
            )}
          </div>
        </section>

        {/* Weekly Reading Goal */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>本周阅读目标</h3>
            <span className="dashboard-card__tag">8 / 12 篇</span>
          </div>
          <div className="dashboard-goal">
            <div className="dashboard-goal__stat">
              <span className="dashboard-goal__number">8</span>
              <span className="dashboard-goal__divider">/</span>
              <span className="dashboard-goal__total">12 篇精读</span>
            </div>
            <div className="dashboard-progress dashboard-progress--lg">
              <div className="dashboard-progress__bar" style={{ width: "67%" }} />
            </div>
            <p className="dashboard-goal__hint">距离本周目标还差 4 篇，保持专注！</p>
            <div className="dashboard-days">
              {["一", "二", "三", "四", "五", "六", "日"].map((day, idx) => (
                <div
                  key={day}
                  className={`dashboard-day ${idx < 4 ? "dashboard-day--done" : idx === 4 ? "dashboard-day--current" : ""}`}
                >
                  <span className="dashboard-day__name">{day}</span>
                  <span className="dashboard-day__dot">{idx < 4 ? "✓" : "·"}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* Recent Notes */}
        <section className="dashboard-card">
          <div className="dashboard-card__header">
            <h3>研究与思考笔记</h3>
            <span className="dashboard-card__tag">最新思考</span>
          </div>
          <div className="dashboard-notes-list">
            <div className="dashboard-note-item">
              <span className="dashboard-note-item__bullet">•</span>
              <div className="dashboard-note-item__content">
                <p>“作者关于多模态融合的注意力机制证明了跨模态对齐的鲁棒性……”</p>
                <time>今天 10:24 · 来自《Multimodal-LLM-Survey.pdf》</time>
              </div>
            </div>
            <div className="dashboard-note-item">
              <span className="dashboard-note-item__bullet">•</span>
              <div className="dashboard-note-item__content">
                <p>“实验部分的数据集消融实验值得在我们的下一阶段研究中借鉴。”</p>
                <time>昨天 16:45 · 来自《Deep-Learning-Basics.pdf》</time>
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
