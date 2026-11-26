import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
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

test("nested dialogs close only the top and restore focus to its opener", () => {
  const outer = vi.fn();
  function Harness() {
    const [inner, setInner] = useState(false);
    return <ModalDialog label="外层" onClose={outer}><button onClick={() => setInner(true)}>打开内层</button>{inner && <ModalDialog label="内层" onClose={() => setInner(false)}><button>内层操作</button></ModalDialog>}</ModalDialog>;
  }
  render(<Harness />);
  const opener = screen.getByRole("button", { name: "打开内层" });
  fireEvent.click(opener);
  expect(screen.getByRole("button", { name: "内层操作" })).toHaveFocus();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(outer).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog", { name: "内层" })).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(outer).toHaveBeenCalledOnce();
});

test("focus stays inside the active dialog and skips hidden or negative-tabindex controls", async () => {
  const user = userEvent.setup();
  render(<><button>背景按钮</button><ModalDialog label="焦点" onClose={vi.fn()}><button hidden>隐藏</button><button tabIndex={-1}>跳过</button><input data-autofocus aria-label="优先输入" /><button>最后按钮</button></ModalDialog></>);
  expect(screen.getByRole("textbox", { name: "优先输入" })).toHaveFocus();
  screen.getByRole("button", { name: "背景按钮" }).focus();
  expect(screen.getByRole("textbox", { name: "优先输入" })).toHaveFocus();
  await user.keyboard("{Shift>}{Tab}{/Shift}");
  expect(screen.getByRole("button", { name: "最后按钮" })).toHaveFocus();
  await user.keyboard("{Tab}");
  expect(screen.getByRole("textbox", { name: "优先输入" })).toHaveFocus();
});
