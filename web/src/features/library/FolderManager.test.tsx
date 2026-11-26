import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { FolderManager } from "./FolderManager";
import type { FolderApi, LibraryFolder } from "./folders";

afterEach(cleanup);
const folder: LibraryFolder = { library_id:"one", display_path:"D:\\papers", recursive:true, document_count:2, scan_state:"completed", last_synced_at:"2026-10-18T05:00:00Z", warnings:[] };
function api(): FolderApi {
  return { list:vi.fn().mockResolvedValue([folder]), select:vi.fn().mockResolvedValue({selection_token:"selected-token",display_path:"D:\\new-papers"}), register:vi.fn().mockResolvedValue(undefined), scan:vi.fn().mockResolvedValue(undefined) };
}

test("requires confirmation and passes recursive preference for the selected folder", async () => {
  const value=api(); const changed=vi.fn(); const user=userEvent.setup();
  render(<FolderManager api={value} onClose={()=>{}} onChanged={changed} />);
  await screen.findByText("D:\\papers");
  await user.click(screen.getByRole("button",{name:"＋ 添加本地文件夹"}));
  await screen.findByText("D:\\new-papers"); expect(value.register).not.toHaveBeenCalled();
  await user.click(screen.getByRole("checkbox",{name:"包含子文件夹"}));
  await user.click(screen.getByRole("button",{name:"确认关联"}));
  await screen.findByText("文件夹已关联，文献将自动出现。");
  expect(value.register).toHaveBeenCalledWith({selection_token:"selected-token",display_path:"D:\\new-papers"},false);
  expect(changed).toHaveBeenCalledOnce();
});

test("cancellation never registers, and consumed selection is discarded on rejection", async () => {
  const value=api(); const user=userEvent.setup();
  vi.mocked(value.register).mockRejectedValue(new Error("overlapping_library_root"));
  render(<FolderManager api={value} onClose={()=>{}} onChanged={()=>{}} />);
  await user.click(screen.getByRole("button",{name:"＋ 添加本地文件夹"}));
  await user.click(await screen.findByRole("button",{name:"取消关联"}));
  expect(value.register).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button",{name:"＋ 添加本地文件夹"}));
  await user.click(await screen.findByRole("button",{name:"确认关联"}));
  await screen.findByRole("alert"); expect(screen.queryByRole("button",{name:"确认关联"})).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("重叠");
});

test("rescans the correct source and retries failed loading", async () => {
  const value=api(); const user=userEvent.setup();
  vi.mocked(value.list).mockRejectedValueOnce(new Error("offline"));
  render(<FolderManager api={value} onClose={()=>{}} onChanged={()=>{}} />);
  await user.click(await screen.findByRole("button",{name:"重试读取"}));
  await user.click(await screen.findByRole("button",{name:"重新扫描 D:\\papers"}));
  await screen.findByText("已安排重新扫描，文献列表会自动更新。");
  expect(value.scan).toHaveBeenCalledWith("one");
});

test("late folder selection after unmount does not register anything", async () => {
  const value=api(); let resolve!: (v:null)=>void;
  vi.mocked(value.select).mockImplementation(()=>new Promise(r=>{resolve=r;}));
  const view=render(<FolderManager api={value} onClose={()=>{}} onChanged={()=>{}} />);
  await userEvent.click(screen.getByRole("button",{name:"＋ 添加本地文件夹"}));
  await waitFor(()=>expect(value.select).toHaveBeenCalledOnce()); view.unmount();
  await act(async()=>resolve(null)); expect(value.register).not.toHaveBeenCalled();
});
