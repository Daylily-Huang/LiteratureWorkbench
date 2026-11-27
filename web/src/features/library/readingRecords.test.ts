import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { readReadingRecords, useReadingRecords } from "./readingRecords";
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });
test("ignores corrupt storage and invalid records", () => {
  localStorage.setItem("lw-reading-v1", "invalid json"); expect(readReadingRecords()).toEqual({});
  localStorage.setItem("lw-reading-v1", JSON.stringify({bad:{page:-1},good:{page:4,totalPages:20,lastOpened:100,favorite:true}}));
  expect(Object.keys(readReadingRecords())).toEqual(["good"]);
});
test("keeps progress when favoriting and restores it in a new session", () => {
  const {result,unmount}=renderHook(useReadingRecords);
  act(()=>result.current.update("doc", r=>({...r,page:12,totalPages:20,lastOpened:123})));
  act(()=>result.current.update("doc", r=>({...r,favorite:true})));
  unmount();
  const restored=renderHook(useReadingRecords);
  expect(restored.result.current.records.doc).toEqual({page:12,totalPages:20,lastOpened:123,favorite:true});
});
test("retains session progress and exposes storage failure", async () => {
  vi.spyOn(Storage.prototype,"setItem").mockImplementation(()=>{throw new DOMException("quota", "QuotaExceededError");});
  const {result}=renderHook(useReadingRecords);
  act(()=>result.current.update("doc", r=>({...r,page:3})));
  expect(result.current.records.doc?.page).toBe(3); await waitFor(() => expect(result.current.storageFailed).toBe(true));
});

test("server records win over stale cache and delayed hydration preserves user edits", async () => {
  localStorage.setItem("lw-reading-v1",JSON.stringify({doc:{page:2,totalPages:20,lastOpened:5,favorite:false}}));
  let resolve!: (response: Response) => void;
  const fetcher=vi.fn((url: string, init?: RequestInit) => {
    if(url.endsWith("/reading")) return new Promise<Response>(r=>{resolve=r;});
    return Promise.resolve(new Response(JSON.stringify(url.endsWith("current") ? {csrf_token:"csrf"} : {})));
  });
  vi.stubGlobal("fetch",fetcher);
  const {result}=renderHook(useReadingRecords);
  act(()=>result.current.update("doc", r=>({...r,favorite:true})));
  await act(async()=>resolve(new Response(JSON.stringify({items:{doc:{page:12,totalPages:20,lastOpened:100,favorite:false,readingStatus:"read",tags:["methods"]}}}))));
  await waitFor(()=>expect(result.current.ready).toBe(true));
  expect(result.current.records.doc).toMatchObject({page:12,favorite:true,tags:["methods"]});
  await waitFor(()=>expect(fetcher.mock.calls.some(c=>c[1]?.method==="PATCH")).toBe(true));
  expect(JSON.parse(fetcher.mock.calls.find(c=>c[1]?.method==="PATCH")![1]!.body as string)).toEqual({favorite:true});
  vi.unstubAllGlobals();
});
