import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AnnotationPanel } from "./AnnotationPanel";
import { exportNotes } from "./exportNotes";
import type { PdfAnnotation } from "./annotations";
afterEach(cleanup);
const item: PdfAnnotation={annotation_id:"ann",page:12,type:"highlight",owned:true,read_only:false,comment:"Check sample size",selected_text:"Original evidence",subject:"",color:[0.35,0.68,0.95],quads:[]};
test("shows the actual saved color and sends only the selected change",()=>{
 const update=vi.fn(); render(<AnnotationPanel document={{documentId:"doc",documentVersionId:"v",contentId:"a",filename:"paper.pdf"}} state={{document_id:"doc",current_document_version_id:"v",content_id:"a",items:[item]}} busy={false} error={null} onDelete={()=>{}} onJump={()=>{}} onRestore={()=>{}} onUndo={()=>{}} onUpdate={update} />);
 const select=screen.getByRole("combobox", {name:"修改高亮颜色"}); expect(select).toHaveValue("blue"); fireEvent.change(select,{target:{value:"green"}}); expect(update).toHaveBeenCalledWith(item,{color:"green"});
});
test("exports exact source text, physical pages and comments without raw HTML",()=>{
 const text=exportNotes("paper.pdf",[{...item,comment:"<script>unsafe</script>"}]);
 expect(text).toContain("第 12 页");expect(text).toContain("> Original evidence");expect(text).toContain("&lt;script&gt;");expect(text).not.toContain("<script>");
});
