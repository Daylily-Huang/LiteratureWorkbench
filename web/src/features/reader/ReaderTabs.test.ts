import { afterEach, expect, test, vi } from "vitest";
import { readReaderTabs, saveReaderTabs } from "./ReaderTabs";

afterEach(() => { sessionStorage.clear(); vi.restoreAllMocks(); });

test("restores valid unique tabs and includes a directly linked document", () => {
  sessionStorage.setItem("lw-reader-tabs-v1", JSON.stringify([
    null, { documentId: "a", title: "A.pdf", scale: 1.5 },
    { documentId: "a", title: "duplicate", scale: 1 },
    { documentId: "b", title: "B.pdf", scale: 99 }, { documentId: 42 },
  ]));
  expect(readReaderTabs("c")).toEqual([
    { documentId: "a", title: "A.pdf", scale: 1.5 },
    { documentId: "b", title: "B.pdf", scale: 1 },
    { documentId: "c", title: "正在读取文献…", scale: 1 },
  ]);
});

test("a damaged or blocked cache never prevents opening a paper", () => {
  sessionStorage.setItem("lw-reader-tabs-v1", "broken{");
  expect(readReaderTabs(null)).toEqual([]);
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  expect(readReaderTabs("a")).toHaveLength(1);
  expect(() => saveReaderTabs([])).not.toThrow();
});
