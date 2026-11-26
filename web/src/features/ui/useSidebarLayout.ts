import { useEffect, useState } from "react";

const KEY = "lw-sidebar-layout-v1";
type Preferences = { libraryWidth: number; assistantWidth: number; libraryExpanded: boolean | null; assistantExpanded: boolean };
const defaults: Preferences = { libraryWidth: 240, assistantWidth: 300, libraryExpanded: null, assistantExpanded: true };
function read(): Preferences {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) ?? "null");
    if (!value || typeof value !== "object") return defaults;
    const width = (raw: unknown, min: number, max: number, fallback: number) => typeof raw === "number" && Number.isFinite(raw) ? Math.min(max, Math.max(min, raw)) : fallback;
    return { libraryWidth: width(value.libraryWidth, 200, 520, 240), assistantWidth: width(value.assistantWidth, 260, 640, 300),
      libraryExpanded: typeof value.libraryExpanded === "boolean" ? value.libraryExpanded : null,
      assistantExpanded: typeof value.assistantExpanded === "boolean" ? value.assistantExpanded : true };
  } catch { return defaults; }
}

export function useSidebarLayout(readingDocument: boolean, focusMode: boolean) {
  const [preferences, setPreferences] = useState(read);
  const [viewport, setViewport] = useState(window.innerWidth);
  useEffect(() => {
    const resize = () => setViewport(window.innerWidth);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify(preferences)); } catch { /* Layout remains usable without browser storage. */ }
  }, [preferences]);
  const libraryVisible = !focusMode && (preferences.libraryExpanded ?? !readingDocument);
  const assistantVisible = !focusMode && preferences.assistantExpanded;
  // Reserve two 24px handles and at least 360px for the central workspace on desktop.
  const available = Math.max(460, viewport - 48 - 360);
  const libraryWidth = Math.min(preferences.libraryWidth, Math.max(200, available - (assistantVisible ? preferences.assistantWidth : 0)));
  const assistantWidth = Math.min(preferences.assistantWidth, Math.max(260, available - (libraryVisible ? libraryWidth : 0)));
  const libraryMax = Math.min(520, available - (assistantVisible ? assistantWidth : 0));
  const assistantMax = Math.min(640, available - (libraryVisible ? libraryWidth : 0));
  return {
    libraryVisible, assistantVisible, libraryWidth, assistantWidth, libraryMax, assistantMax,
    setLibraryExpanded: (expanded: boolean) => setPreferences(p => ({ ...p, libraryExpanded: expanded })),
    setAssistantExpanded: (expanded: boolean) => setPreferences(p => ({ ...p, assistantExpanded: expanded })),
    resize: (side: "library" | "assistant", width: number) => setPreferences(p => ({ ...p,
      [side === "library" ? "libraryWidth" : "assistantWidth"]: Math.round(Math.max(side === "library" ? 200 : 260, Math.min(side === "library" ? libraryMax : assistantMax, width))),
    })),
  };
}
