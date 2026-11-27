import { useEffect } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ResearchStudio, alignedPage } from "./ResearchStudio";
import type { PaneLocation } from "./LinkedPdfPane";
import type { ReaderDocument } from "../reader/PdfReader";
vi.mock("./LinkedPdfPane",()=>({LinkedPdfPane:({side,location,onProgress,onReady}:{side:"left"|"right";location:PaneLocation|null;onProgress(side:string,id:string,page:number,total:number):void;onReady(side:string,doc:ReaderDocument|null):void})=>{
  useEffect(()=>{if(location){onReady(side,{documentId:location.id,documentVersionId:"v",contentId:"a".repeat(64),filename:location.id});onProgress(side,location.id,location.page,12);}},[location?.id,location?.nonce,side,onProgress,onReady]);
  return <div><span>{side}:{location?.id}:{location?.page}</span><button onClick={()=>location&&onProgress(side,location.id,7,12)}>翻到{side}第7页</button></div>;
}}));
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.restoreAllMocks();});
const documents=["a","b"].map(id=>({document_id:id,filename:id+".pdf",relative_folder:"",authorization_source:"test"}));
test("opens directly in relation review for a transferred cached-study draft", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [], nodes: [], edges: [], history_truncated: false }))));
  render(<ResearchStudio initialAnalysisId="transferred" documents={documents} initialIds={["a", "b"]} records={{}} onUpdate={vi.fn()} onAsk={vi.fn()} onClose={vi.fn()} />);
  expect(screen.getByRole("button", { name: "AI 关系审核" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "双栏对照" })).toHaveAttribute("aria-pressed", "false");
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(c => String(c[0]).endsWith("/analyses/transferred"))).toBe(true));
});
test("remembers independent page positions when switching between graph and reading",async()=>{
  vi.stubGlobal("fetch",vi.fn(async(url:string)=>new Response(JSON.stringify(url.endsWith("/graph")?{nodes:[],edges:[],history_truncated:false}:{items:[]}))));
  render(<ResearchStudio documents={documents} initialIds={["a","b"]} records={{}} onUpdate={vi.fn()} onAsk={vi.fn()} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("button",{name:"翻到left第7页"}));
  fireEvent.click(screen.getByRole("button",{name:"知识图谱"}));
  fireEvent.click(screen.getByRole("button",{name:"双栏对照"}));
  expect(await screen.findByText("left:a:7")).toBeVisible();
  expect(screen.getByText("right:b:1")).toBeVisible();
  expect(alignedPage(7,13,7)).toBe(4);expect(alignedPage(1,1,7)).toBe(1);
  await waitFor(()=>expect(vi.mocked(fetch).mock.calls.some(call=>String(call[0]).includes("notebook/index?limit=100"))).toBe(true));
  expect(vi.mocked(fetch).mock.calls.some(call=>String(call[0]).endsWith("/notebook"))).toBe(false);
});
test("refreshing a changed group does not advance the editing revision or discard draft on conflict",async()=>{
  let revision=0;
  const fetcher=vi.fn(async(url:string,init?:RequestInit)=>{
    if(url.endsWith("/session/current"))return new Response(JSON.stringify({csrf_token:"csrf"}));
    if(init?.method==="PATCH")return new Response(JSON.stringify({code:"group_conflict"}),{status:409});
    if(url.endsWith("/groups"))return new Response(JSON.stringify({items:[{id:"g",title:revision?"Changed elsewhere":"Group",document_ids:["a","b"],revision}]}));
    return new Response(JSON.stringify(url.endsWith("/graph")?{nodes:[],edges:[],history_truncated:false}:{items:[]}));
  });vi.stubGlobal("fetch",fetcher);
  render(<ResearchStudio documents={documents} initialIds={[]} records={{}} onUpdate={vi.fn()} onAsk={vi.fn()} onClose={vi.fn()} />);
  await screen.findByRole("option",{name:"Group · 2 篇"});
  fireEvent.change(screen.getByLabelText("打开研读组"),{target:{value:"g"}});
  fireEvent.change(screen.getByLabelText("研读组名称"),{target:{value:"My draft"}});
  revision=1;fireEvent.click(screen.getByRole("button",{name:"刷新关联"}));
  await screen.findByRole("option",{name:"Changed elsewhere · 2 篇"});
  fireEvent.click(screen.getByRole("button",{name:"保存研读组"}));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("研读组名称")).toHaveValue("My draft");
  await waitFor(()=>expect(fetcher.mock.calls.some(c=>c[1]?.method==="PATCH")).toBe(true));
  const call=fetcher.mock.calls.find(c=>c[1]?.method==="PATCH")!;
  expect(JSON.parse(call[1]!.body as string).revision).toBe(0);
});

test("question-only draft is protected and relation payload keeps its exact context and both pages",async()=>{
  let saved=false;
  const fetcher=vi.fn(async(url:string,init?:RequestInit)=>{
    if(url.endsWith("/session/current"))return new Response(JSON.stringify({csrf_token:"csrf"}));
    if(init?.method==="POST"){saved=true;return new Response(JSON.stringify({id:"r"}));}
    if(saved && url.endsWith("/graph"))throw new Error("offline");
    return new Response(JSON.stringify(url.endsWith("/graph")?{nodes:[],edges:[],history_truncated:false}:{items:[]}));
  });vi.stubGlobal("fetch",fetcher);const close=vi.fn();vi.spyOn(window,"confirm").mockReturnValue(false);
  render(<ResearchStudio documents={documents} initialIds={["a","b"]} records={{}} onUpdate={vi.fn()} onAsk={vi.fn()} onClose={close} />);
  fireEvent.click(screen.getByText("手动记录文献关系"));
  fireEvent.change(screen.getByLabelText("关系所属研究问题"),{target:{value:"Do designs matter?"}});
  fireEvent.click(screen.getByRole("button",{name:"关闭研读空间"}));expect(close).not.toHaveBeenCalled();expect(window.confirm).toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("关系依据与适用条件"),{target:{value:"Observed estimates differ"}});
  fireEvent.change(screen.getByLabelText("差异主要来源"),{target:{value:"method"}});
  fireEvent.change(screen.getByLabelText("方法样本与条件对照"),{target:{value:"A repeats sampling; B does not"}});
  fireEvent.click(screen.getByRole("button",{name:"翻到left第7页"}));
  fireEvent.click(screen.getByRole("button",{name:"保存关系"}));
  await screen.findByText("文献关系已保存到图谱（手动判断，尚需核对）。");
  const call=fetcher.mock.calls.find(c=>c[1]?.method==="POST")!;
  expect(JSON.parse(call[1]!.body as string)).toMatchObject({source_id:"a",target_id:"b",source_page:7,target_page:1,question:"Do designs matter?",difference_basis:"method",comparison_note:"A repeats sampling; B does not"});
  expect(screen.getByLabelText("关系所属研究问题")).toHaveValue("");
  expect(await screen.findByRole("alert")).toHaveTextContent("操作已保存，但关联视图暂未刷新");
});
