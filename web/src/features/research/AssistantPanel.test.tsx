import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ReaderDocument } from "../reader/PdfReader";
import { AssistantPanel } from "./AssistantPanel";


const DOCUMENT: ReaderDocument = {
  documentId: "doc-1",
  documentVersionId: "ver-1",
  contentId: "c".repeat(64),
  filename: "paper.pdf",
};

const ANSWER = {
  answer: "Alpha is the first letter [p1].",
  citations: [{ physical_page: 3, excerpt: "Alpha beta gamma", rects: [[10, 20, 300, 80]] }],
  provider: "deepseek",
  model: "deepseek-v4-flash",
  fallback_reason: null,
  citation_status: "verified",
};


describe("AssistantPanel", () => {
  afterEach(cleanup);

  it("defaults to the library scope without an open document", () => {
    render(<AssistantPanel document={null} onJumpToCitation={() => undefined} />);

    expect(screen.getByRole("button", { name: "提问" })).toBeDisabled();
    expect(screen.getByLabelText("向文献库提问")).toBeEnabled();
    expect(screen.getByRole("button", { name: "当前文献" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "整个文献库" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("asks the whole library and renders attributed citations", async () => {
    const libraryAnswer = {
      answer: "库里有 alpha [d1 p1]",
      citations: [
        {
          document_id: "doc-9",
          display_name: "other.pdf",
          physical_page: 2,
          excerpt: "Alpha beta gamma",
          rects: [[10, 20, 300, 80]],
        },
      ],
      provider: "deepseek",
      model: "deepseek-v4-flash",
      fallback_reason: null,
      indexed_documents: 3,
      citation_status: "verified",
    };
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(libraryAnswer), { status: 200 }));
    const onJumpToLibraryCitation = vi.fn();
    render(
      <AssistantPanel
        document={DOCUMENT}
        fetcher={fetcher}
        onJumpToCitation={() => undefined}
        onJumpToLibraryCitation={onJumpToLibraryCitation}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "整个文献库" }));
    fireEvent.change(screen.getByLabelText("向文献库提问"), {
      target: { value: "库里有 alpha 吗？" },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("库里有 alpha [d1 p1]")).toBeVisible();
    expect(screen.getByText("已检索 3 篇文献")).toBeVisible();
    const askCall = fetcher.mock.calls[1];
    expect(askCall?.[0]).toBe("/api/libraries/ask");

    fireEvent.click(screen.getByRole("button", { name: "《other.pdf》第 2 页" }));
    expect(onJumpToLibraryCitation).toHaveBeenCalledWith({
      document_id: "doc-9",
      display_name: "other.pdf",
      physical_page: 2,
      excerpt: "Alpha beta gamma",
      rects: [[10, 20, 300, 80]],
    });
  });

  it("asks the library directly when no document is open", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            answer: "没有命中",
            citations: [],
            provider: "retrieval_only",
            model: "",
            fallback_reason: null,
            indexed_documents: 0,
            citation_status: "missing",
          }),
          { status: 200 },
        ),
      );
    render(<AssistantPanel document={null} fetcher={fetcher} onJumpToCitation={() => undefined} />);
    fireEvent.change(screen.getByLabelText("向文献库提问"), {
      target: { value: "什么是 alpha？" },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("没有命中")).toBeVisible();
    expect(screen.getByText("库内检索")).toBeVisible();
    expect(fetcher.mock.calls[1]?.[0]).toBe("/api/libraries/ask");
  });

  it("asks with csrf and renders the answer with clickable citations", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(ANSWER), { status: 200 }));
    const onJumpToCitation = vi.fn();
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={onJumpToCitation} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "  What is alpha?  " },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("Alpha is the first letter [p1].")).toBeVisible();
    expect(screen.getByText("DeepSeek · deepseek-v4-flash")).toBeVisible();
    const askCall = fetcher.mock.calls[1];
    expect(askCall?.[0]).toBe("/api/documents/doc-1/ask");
    expect(askCall?.[1]).toMatchObject({
      headers: { "Content-Type": "application/json", "X-LW-CSRF": "csrf" },
    });
    expect(JSON.parse(String((askCall?.[1] as RequestInit).body))).toEqual({
      question: "What is alpha?",
    });

    fireEvent.click(screen.getByRole("button", { name: "第 3 页" }));
    expect(onJumpToCitation).toHaveBeenCalledWith({
      physical_page: 3,
      excerpt: "Alpha beta gamma",
      rects: [[10, 20, 300, 80]],
    });
  });

  it("renders notice when answer has missing citations", async () => {
    const missingAnswer = {
      answer: "Alpha without citation.",
      citations: [],
      provider: "deepseek",
      model: "deepseek-v4-flash",
      fallback_reason: null,
      citation_status: "missing",
    };
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(missingAnswer), { status: 200 }));
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "What is alpha?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("Alpha without citation.")).toBeVisible();
    expect(screen.getByText("回答未生成可验证的原文引用，请核对原文。")).toBeVisible();
  });

  it("clears answer when document changes to prevent race/cross-doc pollution", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(ANSWER), { status: 200 }));
    const { rerender } = render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "What is alpha?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("Alpha is the first letter [p1].")).toBeVisible();

    const otherDoc: ReaderDocument = {
      documentId: "doc-2",
      documentVersionId: "ver-2",
      contentId: "d".repeat(64),
      filename: "other.pdf",
    };

    rerender(
      <AssistantPanel document={otherDoc} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );

    expect(screen.queryByText("Alpha is the first letter [p1].")).not.toBeInTheDocument();
  });

  it("syncs draft question passed from external props", () => {
    const onConsumed = vi.fn();
    const { rerender } = render(
      <AssistantPanel
        document={DOCUMENT}
        draftQuestion=""
        onDraftQuestionConsumed={onConsumed}
        onJumpToCitation={() => undefined}
      />,
    );

    rerender(
      <AssistantPanel
        document={DOCUMENT}
        draftQuestion="从 Dashboard 传入的推荐问题"
        onDraftQuestionConsumed={onConsumed}
        onJumpToCitation={() => undefined}
      />,
    );

    expect(screen.getByLabelText("向当前文献提问")).toHaveValue("从 Dashboard 传入的推荐问题");
    expect(onConsumed).toHaveBeenCalled();
  });

  it("flags local-model fallback answers distinctly", async () => {
    const fallbackAnswer = {
      ...ANSWER,
      provider: "qwen",
      model: "qwen-local",
      fallback_reason: "deepseek_total_timeout",
    };
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(fallbackAnswer), { status: 200 }));
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "What is alpha?" },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(
      await screen.findByText("本地模型回答（云端不可用，已自动切换）"),
    ).toBeVisible();
  });

  it("surfaces backend error codes as readable messages", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ code: "parse_required" }), { status: 409 }),
      );
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    fireEvent.change(screen.getByLabelText("向当前文献提问"), {
      target: { value: "What is alpha?" },
    });

    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "文档尚未完成解析，请稍后再提问",
    );
  });

  it("keeps the previous answer out of view after a failed follow-up", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(new Response(JSON.stringify(ANSWER), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf_token: "csrf" })))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ code: "deepseek_total_timeout" }), { status: 502 }),
      );
    render(
      <AssistantPanel document={DOCUMENT} fetcher={fetcher} onJumpToCitation={() => undefined} />,
    );
    const input = screen.getByLabelText("向当前文献提问");
    fireEvent.change(input, { target: { value: "What is alpha?" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));
    expect(await screen.findByText("Alpha is the first letter [p1].")).toBeVisible();

    fireEvent.change(input, { target: { value: "And beta?" } });
    fireEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("回答失败，请重试");
    await waitFor(() => {
      expect(screen.queryByText("Alpha is the first letter [p1].")).not.toBeInTheDocument();
    });
  });
});
