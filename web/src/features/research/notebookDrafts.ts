export type NoteFields = { title: string; note: string; starred: boolean };
export type NotebookDraft = NoteFields & { version: 1; key: string; entryId: string; revision: number; isNew: boolean; savedAt: string };
const prefix = (workspace: string) => `lw-notebook-draft:${workspace}:`;
const event = "lw-notebook-drafts-changed";
export const notebookDraftEvent = event;

export function sameNote(a: NoteFields, b: NoteFields) {
  return a.title.trim() === b.title.trim() && a.note === b.note && a.starred === b.starred;
}
function valid(value: unknown): value is NotebookDraft {
  if (!value || typeof value !== "object") return false;
  const d = value as Partial<NotebookDraft>;
  return d.version === 1 && typeof d.key === "string" && d.key.length <= 100 && typeof d.entryId === "string" && d.entryId.length <= 100 &&
    typeof d.title === "string" && d.title.length <= 120 && typeof d.note === "string" && d.note.length <= 50000 &&
    typeof d.starred === "boolean" && typeof d.isNew === "boolean" && Number.isInteger(d.revision) && d.revision! >= 0 &&
    typeof d.savedAt === "string" && Number.isFinite(Date.parse(d.savedAt));
}
export function readNotebookDrafts(workspace: string): NotebookDraft[] {
  const result: NotebookDraft[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (!key?.startsWith(prefix(workspace))) continue;
    try {
      const value: unknown = JSON.parse(localStorage.getItem(key) ?? "null");
      if (valid(value) && key === prefix(workspace) + value.key) result.push(value);
    } catch { /* A corrupt browser draft must not block reading saved records. */ }
  }
  return result.sort((a, b) => b.savedAt.localeCompare(a.savedAt));
}
export function writeNotebookDraft(workspace: string, value: NotebookDraft): boolean {
  try {
    localStorage.setItem(prefix(workspace) + value.key, JSON.stringify(value));
    window.dispatchEvent(new Event(event)); return true;
  } catch { return false; }
}
export function removeNotebookDraft(workspace: string, draft: NotebookDraft): boolean {
  try {
    const key = prefix(workspace) + draft.key;
    // Another editor may have changed a restored draft. Never discard its newer text.
    const existing = localStorage.getItem(key);
    if (existing && existing === JSON.stringify(draft)) localStorage.removeItem(key);
    window.dispatchEvent(new Event(event)); return true;
  } catch { return false; }
}
