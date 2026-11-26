import { useLayoutEffect, useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { RelationRequestForm } from "./RelationRequestForm";
const ids=['a','b'];
const study={reading_mode:'full_text',text_only:true,coverage:ids.map(id=>({document_id:id,display_name:id+'.pdf',content_id:id.repeat(64),parse_run_id:'run-'+id,total_pages:2,supplied_pages:2,text_characters:1200})),source_versions:{a:'va',b:'vb'},request_bytes:2000,max_request_bytes:262144,cache_status:'miss',api_call_required:true,context_kind:'analysis_reuse',analysis_count:1,source_modes:{a:'analysis_reuse',b:'full_text'},plan_id:'f'.repeat(64)};
afterEach(()=>{cleanup();localStorage.clear();vi.unstubAllGlobals();});
function Form(){const [q,setQ]=useState('Compare methods');return <RelationRequestForm ids={ids} workspace="full-workspace" question={q} onQuestion={setQ} disabled={false} onBusy={vi.fn()} onComplete={vi.fn(async()=>{})}/>}
it('defaults to full text and requires a free scope check before the only model submission',async()=>{
 const fetcher=vi.fn(async(url:string,init?:RequestInit)=>new Response(JSON.stringify(url.endsWith('/current')?{csrf_token:'csrf'}:url.endsWith('/prepare')?study:{request_id:JSON.parse(init!.body as string).request_id,status:'completed',id:'saved',candidate_count:1})));
 vi.stubGlobal('fetch',fetcher);render(<Form/>);
 expect(screen.getByLabelText('关系分析阅读方式')).toHaveValue('full_text');expect(screen.getByRole('button',{name:'分析所选文献关系'})).toBeDisabled();expect(fetcher).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'免费检查全文与复用范围'}));
 await waitFor(()=>expect(screen.getByRole('button',{name:'分析所选文献关系'})).toBeEnabled());
 expect(fetcher.mock.calls.filter(c=>c[0].endsWith('/analyses'))).toHaveLength(0);
 fireEvent.click(screen.getByRole('button',{name:'分析所选文献关系'}));
 await screen.findByText('本次分析已保存，可能已产生 API 费用。');
 const call=fetcher.mock.calls.find(c=>c[0].endsWith('/analyses'))!;
 expect(JSON.parse(call[1]!.body as string)).toMatchObject({question:'Compare methods',document_ids:ids,reading_mode:'full_text',plan_id:study.plan_id,source_versions:study.source_versions,force_refresh:false});
});
it('question and force changes invalidate the old sending plan and require another free check',async()=>{
 vi.stubGlobal('fetch',vi.fn(async(url:string)=>new Response(JSON.stringify(url.endsWith('/current')?{csrf_token:'csrf'}:study))));render(<Form/>);
 fireEvent.click(screen.getByRole('button',{name:'免费检查全文与复用范围'}));await waitFor(()=>expect(screen.getByRole('button',{name:'分析所选文献关系'})).toBeEnabled());
 fireEvent.change(screen.getByLabelText('AI 关系分析问题'),{target:{value:'A different question'}});expect(screen.getByRole('button',{name:'分析所选文献关系'})).toBeDisabled();
 fireEvent.click(screen.getByRole('button',{name:'免费检查全文与复用范围'}));await waitFor(()=>expect(screen.getByRole('button',{name:'分析所选文献关系'})).toBeEnabled());
 fireEvent.click(screen.getByRole('checkbox'));expect(screen.getByRole('button',{name:'分析所选文献关系'})).toBeDisabled();
});
it('a failed free check never falls back to fragments or invokes the model',async()=>{
 const fetcher=vi.fn(async(url:string)=>url.endsWith('/current')?new Response(JSON.stringify({csrf_token:'csrf'})):new Response(JSON.stringify({code:'full_text_not_ready'}),{status:409}));vi.stubGlobal('fetch',fetcher);render(<Form/>);
 fireEvent.click(screen.getByRole('button',{name:'免费检查全文与复用范围'}));expect(await screen.findByRole('alert')).toHaveTextContent('没有调用模型');expect(screen.getByLabelText('关系分析阅读方式')).toHaveValue('full_text');expect(screen.getByRole('button',{name:'分析所选文献关系'})).toBeDisabled();expect(fetcher.mock.calls.some(c=>c[0].endsWith('/analyses'))).toBe(false);
});
it('does not restore an old fragment request as a full-text attempt',async()=>{
 localStorage.setItem('lw-relation-attempt-v1:full-workspace:'+crypto.randomUUID(),JSON.stringify({request_id:crypto.randomUUID(),question:'Compare methods',document_ids:ids,force_refresh:false,created_at:'2026-10-23'}));const fetcher=vi.fn();vi.stubGlobal('fetch',fetcher);render(<Form/>);await Promise.resolve();expect(fetcher).not.toHaveBeenCalled();
});

it('does not submit during the first render of a new workspace before request recovery has reset ownership',async()=>{
 const fetcher=vi.fn(async(url:string,init?:RequestInit)=>new Response(JSON.stringify(url.endsWith('/current')?{csrf_token:'csrf'}:{request_id:JSON.parse(init!.body as string).request_id,status:'completed',id:'saved'})));vi.stubGlobal('fetch',fetcher);
 function Transition({workspace}:{workspace:string}){
  useLayoutEffect(()=>{const b=document.querySelector('button[type="submit"]') as HTMLButtonElement;b.click();},[workspace]);
  return <RelationRequestForm initialReadingMode="retrieval" ids={ids} workspace={workspace} question="Compare" onQuestion={vi.fn()} disabled={false} onBusy={vi.fn()} onComplete={vi.fn(async()=>{})}/>;
 }
 const view=render(<Transition workspace="one"/>);view.rerender(<Transition workspace="two"/>);
 expect(fetcher).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('button',{name:'分析所选文献关系'}));await screen.findByText('本次分析已保存，可能已产生 API 费用。');
 expect(fetcher.mock.calls.filter(c=>c[0].endsWith('/analyses'))).toHaveLength(1);
});
