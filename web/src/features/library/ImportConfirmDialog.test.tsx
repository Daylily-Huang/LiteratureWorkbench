import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { LibraryPanel } from "./LibraryPanel";
import type { ImportApi } from "./dropFiles";

afterEach(cleanup);

function api(): ImportApi {
  return {
    createImportIntent: vi.fn().mockResolvedValue({
      intent_id: "intent-1",
      upload_token: "token-1",
      expires_at: "2026-07-15T09:15:00Z",
    }),
    uploadImport: vi.fn().mockResolvedValue({
      intent_id: "intent-1",
      status: "uploaded",
      document_id: null,
      error_code: null,
      poll_after_ms: 1,
    }),
    getImport: vi.fn()
      .mockResolvedValueOnce({
        intent_id: "intent-1",
        status: "validating",
        document_id: null,
        error_code: null,
        poll_after_ms: 1,
      })
      .mockResolvedValueOnce({
        intent_id: "intent-1",
        status: "completed",
        document_id: "document-1",
        error_code: null,
        poll_after_ms: null,
      }),
    listActiveImports: vi.fn().mockResolvedValue([]),
  };
}

test("PDF bytes do not upload until explicit confirmation", async () => {
  const user = userEvent.setup();
  const importApi = api();
  render(<LibraryPanel importApi={importApi} />);

  const file = new File(["%PDF-1.7\n%%EOF"], "paper.pdf", {
    type: "application/pdf",
  });
  await user.upload(screen.getByLabelText("导入文献"), file);

  expect(screen.getByText("登记后，这篇 PDF 可按需自动用于云端处理")).toBeVisible();
  expect(importApi.createImportIntent).not.toHaveBeenCalled();
  expect(importApi.uploadImport).not.toHaveBeenCalled();

  await user.click(screen.getByRole("button", { name: "确认导入" }));

  expect(importApi.createImportIntent).toHaveBeenCalledTimes(1);
  expect(importApi.uploadImport).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(screen.getByText("导入完成")).toBeVisible());
});

test("cancelling clears the selected File and calls no endpoint", async () => {
  const user = userEvent.setup();
  const importApi = api();
  render(<LibraryPanel importApi={importApi} />);

  await user.upload(
    screen.getByLabelText("导入文献"),
    new File(["%PDF-1.7"], "cancel.pdf", { type: "application/pdf" }),
  );
  await user.click(screen.getByRole("button", { name: "取消" }));

  expect(screen.queryByRole("button", { name: "确认导入" })).not.toBeInTheDocument();
  expect(importApi.createImportIntent).not.toHaveBeenCalled();
  expect(importApi.uploadImport).not.toHaveBeenCalled();
});

test("non-PDF explains the future one-time permission path", async () => {
  const user = userEvent.setup();
  const importApi = api();
  render(<LibraryPanel importApi={importApi} />);

  await user.upload(
    screen.getByLabelText("导入文献"),
    new File(["docx"], "notes.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }),
  );

  expect(
    screen.getByText("首版暂不导入；以后可在单次明确授权后用于云处理"),
  ).toBeVisible();
  expect(screen.queryByText(/永久/)).not.toBeInTheDocument();
  expect(importApi.createImportIntent).not.toHaveBeenCalled();
});

test("refresh resumes current-session active imports from the server inventory", async () => {
  const importApi = api();
  vi.mocked(importApi.listActiveImports).mockResolvedValue([
    {
      intent_id: "intent-resume",
      status: "completed",
      document_id: "document-resume",
      error_code: null,
      poll_after_ms: null,
    },
  ]);

  render(<LibraryPanel importApi={importApi} />);

  await waitFor(() => expect(importApi.listActiveImports).toHaveBeenCalledTimes(1));
  expect(screen.getByText("导入完成")).toBeVisible();
});

test("dropping a PDF with an empty browser MIME still waits for confirmation", () => {
  const importApi = api();
  render(<LibraryPanel importApi={importApi} />);
  const file = new File(["%PDF-1.7"], "dropped.pdf", { type: "" });

  fireEvent.drop(screen.getByLabelText("文献导入区"), {
    dataTransfer: { files: { item: () => file } },
  });

  expect(screen.getByRole("button", { name: "确认导入" })).toBeVisible();
  expect(importApi.createImportIntent).not.toHaveBeenCalled();
  expect(importApi.uploadImport).not.toHaveBeenCalled();
});
