import json
from dataclasses import replace

import pytest

from literature_workbench.features.research.relation_analysis import original_quote, parse_analysis
from literature_workbench.features.research.service import LibraryAnswer, LibraryCitation, ResearchError


def test_quote_matching_returns_original_whitespace_and_rejects_fabrication():
    assert original_quote("Design A uses\n repeated  sampling.", "Design A uses repeated sampling.") == "Design A uses\n repeated  sampling."
    assert original_quote("Design A uses repeated sampling.", "Design A uses single sampling.") is None
    assert original_quote("Tiny quote", "Tiny") is None


def test_invalid_references_quotes_and_cross_scope_candidates_are_excluded():
    a = LibraryCitation("a", "A", 2, "Repeated sampling supports estimation of detection.", content_id="a" * 64)
    b = LibraryCitation("b", "B", 3, "Single sampling conflates detection and abundance.", content_id="b" * 64)
    good = {"source_ref": "[d1 r1 p2]", "target_ref": "[d2 r1 p3]", "source_quote": a.excerpt, "target_quote": b.excerpt,
            "relation": "method", "difference_basis": "method", "rationale": "Compare sampling", "comparison_note": "Repeated versus single", "caveats": ["Check conditions"]}
    candidates = [good, {**good, "target_ref": "[d2 r1 p99]"}, {**good, "target_quote": "Invented evidence that was never in the document"}, {**good, "target_ref": good["source_ref"], "target_quote": a.excerpt}]
    answer = LibraryAnswer(json.dumps({"summary": "Compare", "candidates": candidates}), [], "test", "test", source_fragments={good["source_ref"]: a, good["target_ref"]: b})
    _, warnings, kept = parse_analysis(answer, ["a", "b"])
    assert len(kept) == 1 and kept[0]["source"]["excerpt"] == a.excerpt
    assert any("3 条" in w for w in warnings)
    assert parse_analysis(answer, ["a", "c"])[2] == []
    with pytest.raises(ResearchError, match="invalid_relation_analysis"):
        parse_analysis(replace(answer, answer="{bad}"), ["a", "b"])


