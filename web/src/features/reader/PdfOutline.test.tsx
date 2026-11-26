import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { PdfOutline } from "./PdfOutline";
afterEach(cleanup);
test("resolves named and referenced destinations to physical pages",async()=>{
  const pdf={numPages:12,getOutline:vi.fn().mockResolvedValue([{title:"Methods",dest:"methods",items:[]}]),getDestination:vi.fn().mockResolvedValue([{num:7,gen:0}]),getPageIndex:vi.fn().mockResolvedValue(4)} as unknown as PDFDocumentProxy;
  const jump=vi.fn();
  render(<PdfOutline pdf={pdf} onJump={jump} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button",{name:"Methods"}));
  await waitFor(()=>expect(jump).toHaveBeenCalledWith(5));
});
