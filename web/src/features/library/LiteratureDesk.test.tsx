import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { LiteratureDesk } from "./LiteratureDesk";
afterEach(cleanup);
const docs=Array.from({length:9},(_,i)=>({document_id:`d${i}`,filename:`Paper ${i}.pdf`,relative_folder:"",authorization_source:"test"}));

test("opens the selection as reading tabs without requesting AI analysis",()=>{
  const open=vi.fn(); const compare=vi.fn();
  render(<LiteratureDesk documents={docs} records={{}} selected={["d0","d1"]} onSelect={vi.fn()} onOpen={vi.fn()} onOpenSelected={open} onCompare={compare} onUpdate={vi.fn()} />);
  fireEvent.click(screen.getByRole("button",{name:"在标签中打开所选"}));
  expect(open).toHaveBeenCalledOnce();
  expect(compare).not.toHaveBeenCalled();
});
test("limits selection to eight and keeps hidden selection explicit",()=>{
  const select=vi.fn();
  render(<LiteratureDesk documents={docs} records={{}} selected={docs.slice(0,8).map(d=>d.document_id)} onSelect={select} onOpen={vi.fn()} onCompare={vi.fn()} onUpdate={vi.fn()} />);
  expect(screen.getByLabelText("选择 Paper 8.pdf")).toBeDisabled();
  fireEvent.change(screen.getByLabelText("搜索文献与标签"),{target:{value:"Paper 8"}});
  expect(screen.getByText(/已选 8 \/ 8 篇/)).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"Paper 0.pdf ×"}));
  expect(select).toHaveBeenCalledWith(docs.slice(1,8).map(d=>d.document_id));
});
