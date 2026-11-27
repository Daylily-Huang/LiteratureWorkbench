import { useCallback, useEffect, useRef, useState } from "react";
import { workspaceRequest } from "../research/workspaceApi";

export type ReadingRecord = { page: number; totalPages: number; lastOpened: number; favorite: boolean; readingStatus?: "unread" | "reading" | "read"; tags?: string[] };
export type ReadingRecords = Record<string, ReadingRecord>;
const KEY = "lw-reading-v1";
export const EMPTY_READING: ReadingRecord = { page: 1, totalPages: 0, lastOpened: 0, favorite: false };

export function readReadingRecords(): ReadingRecords {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(KEY) ?? "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).flatMap(([id, raw]) => {
      if (!raw || typeof raw !== "object") return [];
      const r = raw as ReadingRecord;
      if (!Number.isSafeInteger(r.page) || r.page < 1 || !Number.isSafeInteger(r.totalPages) || r.totalPages < 0
        || !Number.isFinite(r.lastOpened) || r.lastOpened < 0 || typeof r.favorite !== "boolean") return [];
      return [[id, { page: r.page, totalPages: r.totalPages, lastOpened: r.lastOpened, favorite: r.favorite,
        ...(r.readingStatus && ["unread", "reading", "read"].includes(r.readingStatus) ? { readingStatus: r.readingStatus } : {}),
        ...(Array.isArray(r.tags) ? { tags: r.tags.filter(t => typeof t === "string").slice(0, 20) } : {}),
      }]];
    }));
  } catch { return {}; }
}

export function useReadingRecords() {
  const [records, setRecords] = useState(readReadingRecords);
  const recordsRef = useRef(records);
  const [storageFailed, setStorageFailed] = useState(false);
  const [ready, setReady] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const pending = useRef<Record<string, Partial<ReadingRecord>>>({});
  const queues = useRef<Record<string, Promise<void>>>({});
  const hydrated = useRef(false);
  const cache = useCallback(() => {
    try { localStorage.setItem(KEY, JSON.stringify(recordsRef.current)); } catch { /* Server is authoritative. */ }
  }, []);
  const flush = useCallback((id: string) => {
    queues.current[id] = (queues.current[id] ?? Promise.resolve()).then(async () => {
      const patch = pending.current[id];
      if (!patch) return;
      delete pending.current[id];
      try {
        await workspaceRequest(`reading/${encodeURIComponent(id)}`, patch, "PATCH");
        setStorageFailed(Object.keys(pending.current).length > 0);
      } catch {
        pending.current[id] = { ...patch, ...pending.current[id] };
        setStorageFailed(true);
      }
    });
  }, []);
  useEffect(() => {
    let active = true;
    void workspaceRequest<{ items: ReadingRecords }>("reading").then(data => {
      if (!active) return;
      if (!data.items || Array.isArray(data.items)) throw new Error("invalid_records");
      // Migrate only records absent on the server. Never replace newer server records with a stale browser cache.
      for (const [id, record] of Object.entries(recordsRef.current)) {
        if (!data.items[id]) pending.current[id] = { ...record, readingStatus: record.readingStatus ?? (record.lastOpened ? "reading" : "unread"), ...pending.current[id] };
      }
      const merged = { ...data.items };
      for (const [id, patch] of Object.entries(pending.current)) merged[id] = { ...(merged[id] ?? EMPTY_READING), ...patch };
      recordsRef.current = merged;
      setRecords(merged); cache();
      hydrated.current = true;
      setStorageFailed(false);
      for (const id of Object.keys(pending.current)) flush(id);
    }).catch(() => { if (active) setStorageFailed(true); }).finally(() => { if (active) setReady(true); });
    return () => { active = false; };
  }, [cache, flush, attempt]);
  const update = useCallback((id: string, change: (current: ReadingRecord) => ReadingRecord) => {
    const current = recordsRef.current;
    const before = current[id] ?? EMPTY_READING;
    const changed = change(before);
    const patch = Object.fromEntries(Object.entries(changed).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(before[key as keyof ReadingRecord]))) as Partial<ReadingRecord>;
    if (!Object.keys(patch).length) return;
    const next = { ...current, [id]: changed };
    recordsRef.current = next;
    setRecords(next);
    cache();
    pending.current[id] = { ...pending.current[id], ...patch };
    if (hydrated.current) flush(id);
  }, [cache, flush]);
  const retry = useCallback(() => {
    if (!hydrated.current) { setAttempt(n => n + 1); return; }
    for (const id of Object.keys(pending.current)) flush(id);
  }, [flush]);
  return { records, storageFailed, update, ready, retry };
}
