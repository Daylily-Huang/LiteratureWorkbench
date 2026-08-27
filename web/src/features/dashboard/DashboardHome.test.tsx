import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DashboardHome } from "./DashboardHome";
import type { DocumentListItem } from "../library/documents";


describe("DashboardHome", () => {
  afterEach(cleanup);

  it("renders honest empty state when no documents exist", () => {
    const onOpenDocument = vi.fn();
    const onTriggerImport = vi.fn();

    const { getByText, queryByText } = render(
      <DashboardHome
        documents={[]}
        onOpenDocument={onOpenDocument}
        onTriggerImport={onTriggerImport}
      />,
    );

    expect(getByText("0")).toBeInTheDocument();
    expect(getByText("暂无正在阅读的文献")).toBeInTheDocument();
    expect(getByText("文献库当前为空，点击下方按钮或拖入 PDF 开始阅读")).toBeInTheDocument();

    // Verify fabricated stats and fake notes do not exist
    expect(queryByText("65%")).toBeNull();
    expect(queryByText(/8 \/ 12/)).toBeNull();
    expect(queryByText(/Multimodal-LLM-Survey/)).toBeNull();
    expect(queryByText(/Deep-Learning-Basics/)).toBeNull();

    // Trigger import from hero CTA
    const heroBtn = getByText("导入文献开始阅读");
    fireEvent.click(heroBtn);
    expect(onTriggerImport).toHaveBeenCalledTimes(1);
  });

  it("renders real documents and handles navigation and prompt questions", () => {
    const onOpenDocument = vi.fn();
    const onAskQuestion = vi.fn();

    const sampleDocs: DocumentListItem[] = [
      {
        document_id: "doc-1",
        filename: "attention-is-all-you-need.pdf",
        relative_folder: "NLP",
        authorization_source: "manual",
      },
      {
        document_id: "doc-2",
        filename: "bert-paper.pdf",
        relative_folder: "Transformers",
        authorization_source: "manual",
      },
    ];

    const { getAllByText, getByText } = render(
      <DashboardHome
        documents={sampleDocs}
        onOpenDocument={onOpenDocument}
        onAskQuestion={onAskQuestion}
      />,
    );

    expect(getAllByText("2")[0]).toBeInTheDocument();
    expect(getByText("篇已收录文献")).toBeInTheDocument();
    const firstDoc = getAllByText("attention-is-all-you-need.pdf")[0];
    expect(firstDoc).toBeInTheDocument();

    // Click document in reading list
    if (firstDoc) {
      fireEvent.click(firstDoc);
    }
    expect(onOpenDocument).toHaveBeenCalledWith("doc-1");

    // Click prompt question
    const firstPrompt = getByText("这篇文献的核心研究问题与主要贡献是什么？");
    fireEvent.click(firstPrompt);
    expect(onAskQuestion).toHaveBeenCalledWith("这篇文献的核心研究问题与主要贡献是什么？");

    // Switch question set
    fireEvent.click(getByText("换一批"));
    expect(getByText("论文的核心论点与理论假设是什么？")).toBeInTheDocument();
  });
});
