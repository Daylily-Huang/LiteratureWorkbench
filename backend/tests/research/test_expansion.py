from __future__ import annotations

from literature_workbench.features.research.expansion import (
    build_expansion_prompt,
    contains_cjk,
    parse_expansion_response,
)


def test_contains_cjk_detects_chinese_questions() -> None:
    assert contains_cjk("这篇文献讲了什么？")
    assert contains_cjk("Tragulus 的分布范围")
    assert not contains_cjk("What is alpha?")
    assert not contains_cjk("")


def test_expansion_prompt_embeds_question_and_overview_instruction() -> None:
    prompt = build_expansion_prompt("这篇文献的主题是什么？")

    assert "这篇文献的主题是什么？" in prompt
    assert "OVERVIEW" in prompt
    assert "comma" in prompt


def test_parse_overview_marker() -> None:
    expansion = parse_expansion_response("OVERVIEW")

    assert expansion is not None
    assert expansion.kind == "overview"
    assert expansion.match_query is None


def test_parse_overview_tolerates_case_and_trailing_text() -> None:
    expansion = parse_expansion_response("overview — the user asked for a summary")

    assert expansion is not None
    assert expansion.kind == "overview"


def test_parse_keywords_builds_fts_match_query() -> None:
    expansion = parse_expansion_response("camera-trap, Tragulus versicolor, Gia Lai")

    assert expansion is not None
    assert expansion.kind == "keywords"
    assert expansion.match_query == (
        '"camera-trap" OR "Tragulus versicolor" OR "Gia Lai"'
    )


def test_parse_filters_junk_terms_and_dedupes() -> None:
    expansion = parse_expansion_response(
        "alpha, !!, <script>alert(1)</script>, Alpha is here, beta, beta."
    )

    assert expansion is not None
    # "!!" and the script tag are unusable; the trailing-period duplicate of
    # beta collapses into the first occurrence.
    assert expansion.match_query == '"alpha" OR "Alpha is here" OR "beta"'


def test_parse_uses_only_first_non_empty_line() -> None:
    expansion = parse_expansion_response("\nalpha, beta\nThese are the keywords.")

    assert expansion is not None
    assert expansion.match_query == '"alpha" OR "beta"'


def test_parse_caps_term_count() -> None:
    expansion = parse_expansion_response(
        "t1, t2, t3, t4, t5, t6, t7, t8, t9, t10"
    )

    assert expansion is not None
    assert expansion.match_query is not None
    assert expansion.match_query.count("OR") == 7


def test_parse_rejects_unusable_replies() -> None:
    assert parse_expansion_response("我无法理解这个问题") is None
    assert parse_expansion_response("") is None
    assert parse_expansion_response("\n\n  \n") is None
    assert parse_expansion_response("!!! ??? ...") is None
