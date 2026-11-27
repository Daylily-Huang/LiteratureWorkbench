import { describe, expect, it } from "vitest";
import { cleanPdfCopyText } from "./copyText";

describe("clipboard layout cleanup", () => {
  it("joins wrapped text and words while keeping paragraphs", () => {
    expect(cleanPdfCopyText("We identify individu-\nals in natural\npopulations.\n\nNext paragraph."))
      .toBe("We identify individuals in natural populations.\n\nNext paragraph.");
    expect(cleanPdfCopyText("We identify individu-\nals.", "We identify individu-\nals."))
      .toBe("We identify individuals.");
    expect(cleanPdfCopyText("individu-\nals", "individu-als")).toBe("individuals");
  });

  it("keeps ordinary compounds, identifiers, ranges and mathematical symbols", () => {
    expect(cleanPdfCopyText("camera-trap long-\nterm non-\ninvasive DNA-\nbased 2017-\n2020; p < 0.05; x² ± σ"))
      .toBe("camera-trap long-term non-invasive DNA-based 2017-2020; p < 0.05; x² ± σ");
    expect(cleanPdfCopyText("well-\nbeing\n\nindividu-\n\nals")).toBe("well-being\n\nindividu-\n\nals");
  });

  it("uses full spellings on the page, removes soft hyphens and fixes ligatures", () => {
    expect(cleanPdfCopyText("anti-\nbody", "The antibody was measured. anti-\nbody"))
      .toBe("antibody");
    expect(cleanPdfCopyText("identi\u00ad\nfication oﬀers ﬁne results."))
      .toBe("identification offers fine results.");
  });

  it("joins Chinese layout lines and preserves explicit lists", () => {
    expect(cleanPdfCopyText("检测种群\r\n数量变化。\r\n\r\n1. 方法\n2. 结果"))
      .toBe("检测种群数量变化。\n\n1. 方法\n2. 结果");
  });
});
