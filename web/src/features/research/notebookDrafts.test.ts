import { afterEach, expect, test, vi } from "vitest";
import { readNotebookDrafts, removeNotebookDraft, writeNotebookDraft, type NotebookDraft } from "./notebookDrafts";
afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); });
const draft: NotebookDraft = { version: 1, key: "window-a", entryId: "entry", title: "My note", note: "Evidence", starred: true, isNew: false, revision: 2, savedAt: "2026-10-23T00:00:00Z" };
test("isolates workspaces and editor drafts and cannot remove newer text with an old acknowledgement", () => {
  expect(writeNotebookDraft("main", draft)).toBe(true);
  writeNotebookDraft("recovery", { ...draft, note: "Recovered workspace" });
  writeNotebookDraft("main", { ...draft, key: "window-b", note: "Other editor" });
  expect(readNotebookDrafts("main")).toHaveLength(2);
  const newer = { ...draft, note: "Still typing" };
  writeNotebookDraft("main", newer); removeNotebookDraft("main", draft);
  expect(readNotebookDrafts("main").find(d => d.key === "window-a")?.note).toBe("Still typing");
  removeNotebookDraft("main", newer);
  expect(readNotebookDrafts("main")).toHaveLength(1);
  expect(readNotebookDrafts("recovery")[0]?.note).toBe("Recovered workspace");
});
test("corrupt browser records are skipped and quota failures do not claim success", () => {
  localStorage.setItem("lw-notebook-draft:main:bad", "{broken");
  localStorage.setItem("lw-notebook-draft:main:huge", JSON.stringify({ ...draft, key: "huge", note: "x".repeat(50001) }));
  expect(readNotebookDrafts("main")).toEqual([]);
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("quota", "QuotaExceededError"); });
  expect(writeNotebookDraft("main", draft)).toBe(false);
});
