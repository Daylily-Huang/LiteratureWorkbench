import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useAnnotatedDocument } from "./useAnnotatedDocument";

const identity = (id: string, version = "v1") => ({ document_id: id, current_document_version_id: version, content_id: `${id}-${version}` });
function mockDocuments() {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input); const id = url.split("/")[3]!;
    return new Response(JSON.stringify({ ...identity(id), filename: `${id}.pdf`, items: [] }));
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test("writing to the right pane updates only its version and annotations", async () => {
  const fetcher = mockDocuments();
  const original = fetcher.getMockImplementation()!;
  const { result } = renderHook(() => ({ left: useAnnotatedDocument("a"), right: useAnnotatedDocument("b") }));
  await waitFor(() => expect(result.current.right.annotations?.document_id).toBe("b"));
  fetcher.mockImplementation(async input => String(input).endsWith("b/annotations")
    ? new Response(JSON.stringify({ ...identity("b", "v2"), items: [] })) : original(input));
  await act(() => result.current.right.runMutation(async () => identity("b", "v2")));
  expect(result.current.right.document?.documentVersionId).toBe("v2");
  expect(result.current.left.document?.documentVersionId).toBe("v1");
  expect(result.current.left.annotations?.document_id).toBe("a");
});

test("a delayed mutation cannot overwrite a new session even after A to B to A", async () => {
  mockDocuments();
  let finish!: (value: ReturnType<typeof identity>) => void;
  const deferred = new Promise<ReturnType<typeof identity>>(resolve => { finish = resolve; });
  const { result, rerender } = renderHook(({ id }) => useAnnotatedDocument(id), { initialProps: { id: "a" } });
  await waitFor(() => expect(result.current.annotations?.document_id).toBe("a"));
  let pending!: Promise<void>;
  act(() => { pending = result.current.runMutation(() => deferred); });
  rerender({ id: "b" });
  await waitFor(() => expect(result.current.document?.documentId).toBe("b"));
  rerender({ id: "a" });
  await waitFor(() => expect(result.current.document?.documentId).toBe("a"));
  await act(async () => { finish(identity("a", "stale")); await pending; });
  expect(result.current.document?.documentVersionId).toBe("v1");
  expect(result.current.busy).toBe(false);
});

test("serializes writes per pane and rejects another document's mutation response", async () => {
  mockDocuments();
  const { result } = renderHook(() => useAnnotatedDocument("a"));
  await waitFor(() => expect(result.current.document).not.toBeNull());
  const second = vi.fn(async () => identity("a"));
  await act(async () => {
    const pending = result.current.runMutation(async () => identity("wrong"));
    await result.current.runMutation(second);
    await pending;
  });
  expect(second).not.toHaveBeenCalled();
  expect(result.current.error).toBe("annotation_identity_mismatch");
  expect(result.current.document?.documentVersionId).toBe("v1");
});

test("an initial annotation read cannot replace refreshed state after a write", async () => {
  const fetcher = mockDocuments();
  let finish!: (value: Response) => void;
  let reads = 0;
  const deferred = new Promise<Response>(resolve => { finish = resolve; });
  fetcher.mockImplementation(async input => {
    if (String(input).endsWith("/annotations") && ++reads === 1) return deferred;
    return new Response(JSON.stringify({ ...identity("a", reads > 1 ? "v2" : "v1"), filename: "A.pdf", items: [] }));
  });
  const { result } = renderHook(() => useAnnotatedDocument("a"));
  await waitFor(() => expect(result.current.document).not.toBeNull());
  await act(() => result.current.runMutation(async () => identity("a", "v2")));
  await act(async () => { finish(new Response(JSON.stringify({ ...identity("a"), items: [] }))); });
  expect(result.current.annotations?.current_document_version_id).toBe("v2");
});
