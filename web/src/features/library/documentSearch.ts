import type { DocumentListItem } from "./documents";
import type { ReadingRecords } from "./readingRecords";

export function searchText(value: string): string {
  return value.normalize("NFKD").replace(/\p{M}/gu, "").toLocaleLowerCase().replace(/[\p{P}\p{S}]+/gu, " ").replace(/\s+/g, " ").trim();
}
export function documentMatches(document: DocumentListItem, records: ReadingRecords, query: string): boolean {
  const text = searchText([document.filename, document.relative_folder, document.library_name ?? "", ...(records[document.document_id]?.tags ?? [])].join(" "));
  return searchText(query).split(" ").filter(Boolean).every(word => text.includes(word));
}
export function findDocuments(documents: DocumentListItem[], records: ReadingRecords, query: string, openIds: readonly string[] = []): DocumentListItem[] {
  const normalized = searchText(query);
  const open = new Set(openIds);
  const score = (d: DocumentListItem) => {
    const name = searchText(d.filename.replace(/\.pdf$/i, ""));
    return normalized ? (name === normalized ? 100 : name.startsWith(normalized) ? 60 : name.includes(normalized) ? 30 : 0) : 0;
  };
  return documents.filter(d => documentMatches(d, records, query)).map(doc => ({ doc, score: score(doc) })).sort((a, b) => b.score - a.score
    || Number(open.has(b.doc.document_id)) - Number(open.has(a.doc.document_id))
    || (records[b.doc.document_id]?.lastOpened ?? 0) - (records[a.doc.document_id]?.lastOpened ?? 0)
    || Number(records[b.doc.document_id]?.favorite ?? false) - Number(records[a.doc.document_id]?.favorite ?? false)
    || a.doc.filename.localeCompare(b.doc.filename) || a.doc.document_id.localeCompare(b.doc.document_id)).map(item => item.doc);
}
