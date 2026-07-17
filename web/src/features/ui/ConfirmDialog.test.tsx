import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "./ConfirmDialog";


describe("ConfirmDialog", () => {
  afterEach(cleanup);

  it("requires the exact filename for destructive restore", () => {
    const confirm = vi.fn();
    render(<ConfirmDialog description="恢复" expectedText="paper.pdf" label="恢复原始 PDF" onCancel={() => undefined} onConfirm={confirm} />);
    const button = screen.getByRole("button", { name: "确认" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("输入文件名以确认"), { target: { value: "paper.pdf" } });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(confirm).toHaveBeenCalledOnce();
  });

  it("closes with Escape", () => {
    const cancel = vi.fn();
    render(<ConfirmDialog description="删除" label="删除批注" onCancel={cancel} onConfirm={() => undefined} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(cancel).toHaveBeenCalledOnce();
  });
});
