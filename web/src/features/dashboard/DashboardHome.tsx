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
            <span className="dashboard-card__tag">最近文献</span>
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
                    <p>{doc.relative_folder || "默认文件夹"}</p>
                  </div>
                  <span className="dashboard-card__link">阅读 →</span>
                </button>
              ))
            ) : (
              <div className="dashboard-empty-card">
                <p>暂无正在阅读的文献</p>
                <small>导入文献后可在此处快速恢复阅读</small>
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
                ? "文献库运行正常，支持单篇精读与跨文献 RAG 检索"
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
                <p><strong>PDF 精读与划选翻译</strong>：支持双语平行翻译与批注高亮</p>
                <time>深度阅读模式</time>
              </div>
            </div>
            <div className="dashboard-note-item">
              <span className="dashboard-note-item__bullet">•</span>
              <div className="dashboard-note-item__content">
                <p><strong>证据溯源与问答</strong>：单篇与跨文献 RAG，点击页码标签精准高亮定位</p>
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
