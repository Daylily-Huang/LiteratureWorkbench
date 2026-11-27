import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { PdfSearch } from "./PdfSearch";
afterEach(cleanup);
test("finds text on unrendered pages and jumps to the physical page", async()=>{
 const pdf={numPages:3,getPage:vi.fn(async(page:number)=>({getTextContent:async()=>({items:[{str:page===3?"Camera traps reveal ecological patterns":"Other material"}]})}))} as unknown as PDFDocumentProxy;
 const jump=vi.fn(); render(<PdfSearch pdf={pdf} onJump={jump} onClose={()=>{}} />);
 fireEvent.change(screen.getByLabelText("查找正文内容"),{target:{value:"camera traps"}});
 const hit=await screen.findByRole("button",{name:/第 3 页/}); fireEvent.click(hit); expect(jump).toHaveBeenCalledWith(3);
 expect(screen.getByRole("status")).toHaveTextContent("1 个匹配页");
});
test("does not show a late result for a replaced query",async()=>{
 let release!: (value: unknown)=>void;
 const slow=new Promise(resolve=>{release=resolve;});
 const getTextContent=vi.fn().mockReturnValueOnce(slow).mockResolvedValue({items:[{str:"beta"}]});
 const pdf={numPages:1,getPage:vi.fn(async()=>({getTextContent}))} as unknown as PDFDocumentProxy;
 render(<PdfSearch pdf={pdf} onJump={()=>{}} onClose={()=>{}} />);
 fireEvent.change(screen.getByLabelText("查找正文内容"),{target:{value:"alpha"}});
 await waitFor(()=>expect(getTextContent).toHaveBeenCalledTimes(1));
 fireEvent.change(screen.getByLabelText("查找正文内容"),{target:{value:"beta"}});
 await screen.findByRole("button",{name:/beta/});
 await act(async()=>release({items:[{str:"alpha"}]}));
 expect(screen.queryByRole("button",{name:/alpha/})).not.toBeInTheDocument();
});
