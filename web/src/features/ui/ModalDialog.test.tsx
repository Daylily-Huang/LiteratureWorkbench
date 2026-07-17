import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { ModalDialog } from "./ModalDialog";

afterEach(cleanup);

test("rerenders keep focus in place and Escape uses the latest close callback", async () => {
  const firstClose = vi.fn();
  const latestClose = vi.fn();
  const user = userEvent.setup();
  const view = render(
    <ModalDialog label="测试对话框" onClose={firstClose}>
      <button type="button">第一个操作</button>
      <button type="button">第二个操作</button>
    </ModalDialog>,
  );

  const second = screen.getByRole("button", { name: "第二个操作" });
  second.focus();
  view.rerender(
    <ModalDialog label="测试对话框" onClose={latestClose}>
      <button type="button">第一个操作</button>
      <button type="button">第二个操作</button>
    </ModalDialog>,
  );

  expect(second).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(firstClose).not.toHaveBeenCalled();
  expect(latestClose).toHaveBeenCalledTimes(1);
});
