// Layout cleanup for the clipboard only; evidence/translation source text stays unchanged.
const COMPOUND_PREFIXES = new Set((
  "all anti camera capture case closed co cost cross data day dose double end evidence field five four " +
  "full genome half high ill individual large life long low mark method model multi non old open " +
  "peer population post pre real release research risk sample self short single site six small " +
  "space species state study three time two well whole wide world year"
).split(" "));

const LIGATURES: Record<string, string> = {
  "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
};

function joinHyphen(left: string, hyphen: string, right: string, words: Set<string>): string {
  const joined = left + right;
  // Preserve identifiers, ranges, proper names, and non-breaking hyphens.
  if (hyphen === "‑" || !/^[a-z]{2,}$/.test(left) || !/^[a-z]{2,}$/.test(right)) {
    return left + hyphen + right;
  }
  // A complete spelling elsewhere on this page is stronger evidence than a guess.
  if (words.has(joined)) return joined;
  if (COMPOUND_PREFIXES.has(left) || words.has(left)) return left + hyphen + right;
  return joined;
}

export function cleanPdfCopyText(raw: string, pageText = ""): string {
  const completeWords = pageText.replace(/\b[a-z]+[-‐‑\u00ad](?:[\t ]*\n)?/g, "\n");
  const words = new Set(completeWords.match(/\b[a-z]{2,}\b/g) ?? []);
  let text = raw.replace(/\r\n?/g, "\n").replace(/[ﬀﬁﬂﬃﬄﬅﬆ]/g, char => LIGATURES[char]!);
  text = text.replace(/\u00ad[\t ]*\n[\t ]*(?=\p{L})/gu, "").replace(/\u00ad/g, "");
  text = text.replace(/([\p{L}\p{N}]+)([-‐‑])[\t ]*\n[\t ]*([\p{L}\p{N}]+)/gu,
    (_, left: string, hyphen: string, right: string) => joinHyphen(left, hyphen, right, words));
  return text.trim().split(/\n[\t ]*\n+/).map(paragraph => {
    const lines = paragraph.split("\n").map(line => line.replace(/[\t\u00a0 ]+/g, " ").trim()).filter(Boolean);
    let result = lines[0] ?? "";
    for (const line of lines.slice(1)) {
      const listItem = /^(?:[-*•]|\d+[.)])\s/.test(line);
      const cjkJoin = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}，。！？、；：]$/u.test(result)
        && /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(line);
      const punctuation = /^[,.;:!?%\)\]，。！？、；：]/u.test(line);
      result += (listItem ? "\n" : cjkJoin || punctuation ? "" : " ") + line;
    }
    return result;
  }).join("\n\n");
}
