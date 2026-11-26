import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PdfToolbar } from "./PdfToolbar";
afterEach(cleanup);
test("accepts a multi-digit page without navigating on the first digit", () => {
 const jump=vi.fn(); render(<PdfToolbar currentPage={3} totalPages={120} scale={1} onPageChange={jump} onScaleChange={()=>{}} onFitWidth={()=>{}} />);
 const input=screen.getByLabelText("PDF 页码");
 fireEvent.change(input,{target:{value:"1"}}); fireEvent.change(input,{target:{value:"12"}});
 expect(jump).not.toHaveBeenCalled(); fireEvent.keyDown(input,{key:"Enter"}); expect(jump).toHaveBeenCalledWith(12);
 fireEvent.change(input,{target:{value:"999"}}); fireEvent.blur(input); expect(input).toHaveValue(3); expect(jump).toHaveBeenCalledTimes(1);
});
