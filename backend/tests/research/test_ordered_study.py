"""Order-dependent research questions must never reuse a reversed conclusion."""

import json

import pytest

from literature_workbench.features.research.full_text import FullTextError
from literature_workbench.features.research.study_cache import digest
from test_library_service import FakeDeepSeek, _comparison_result, _seed_document, _service


def warm(database):
    a = _seed_document(
        database,
        content_id="a" * 64,
        display_name="A.pdf",
        page_texts=[["Alpha verified source quotation."]],
    )
    b = _seed_document(
        database,
        content_id="b" * 64,
        display_name="B.pdf",
        page_texts=[["Beta verified source quotation."]],
    )
    question = "Does the first paper support the second paper?"
    model = FakeDeepSeek(
        result=_comparison_result(
            [
                ("[d1 p1]", "Alpha verified source quotation."),
                ("[d2 p1]", "Beta verified source quotation."),
            ],
            "Original first-to-second conclusion",
        )
    )
    service = _service(database, model)
    plan = service.prepare_full_text(question=question, document_ids=[a, b])
    original = service.ask_full_text(
        question=question,
        document_ids=[a, b],
        source_versions=plan["source_versions"],
        plan_id=plan["plan_id"],
    )
    return a, b, question, service, model, original


def test_each_paper_order_keeps_its_own_answer_and_repeats_are_free(research_db):
    a, b, question, service, model, original = warm(research_db)
    plan = service.prepare_full_text(question=question, document_ids=[b, a])
    assert plan["cache_status"] == "miss" and plan["api_call_required"] is True
    assert plan["source_modes"] == {b: "analysis_reuse", a: "analysis_reuse"}
    assert len(model.calls) == 1
    model.result = _comparison_result(
        [
            ("[d1 p1]", "Beta verified source quotation."),
            ("[d2 p1]", "Alpha verified source quotation."),
        ],
        "Reversed first-to-second conclusion",
    )
    reversed_answer = service.ask_full_text(
        question=question,
        document_ids=[b, a],
        source_versions=plan["source_versions"],
        plan_id=plan["plan_id"],
    )
    payload = json.loads(model.calls[-1])
    assert "new_documents" not in payload
    assert all(not note["analysis"]["claims"] for note in payload["previous_studies"])
    assert all(note["previous_question"] != question for note in payload["previous_studies"])
    assert [(c.document_id, c.excerpt) for c in reversed_answer.citations] == [
        (b, "Beta verified source quotation."),
        (a, "Alpha verified source quotation."),
    ]
    for ids, expected in [([a, b], original.answer), ([b, a], reversed_answer.answer)]:
        again = service.prepare_full_text(question=question, document_ids=ids)
        assert again["cache_status"] == "hit" and again["api_call_required"] is False
        answer = service.ask_full_text(
            question=question,
            document_ids=ids,
            source_versions=again["source_versions"],
            plan_id=again["plan_id"],
        )
        assert answer.answer == expected
    assert len(model.calls) == 2
    with research_db.connect() as c:
        assert c.execute("SELECT count(*) FROM full_text_study_cache").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM document_relations").fetchone()[0] == 0


def test_original_order_legacy_paid_answer_stays_free_but_reverse_is_not_an_exact_hit(research_db):
    a, b, question, service, model, original = warm(research_db)
    with research_db.transaction() as c:
        row = c.execute("SELECT * FROM full_text_study_cache").fetchone()
        legacy_key = digest([row["scope_key"], question])
        c.execute("DELETE FROM full_text_study_documents")
        c.execute("UPDATE full_text_study_cache SET cache_key=?", (legacy_key,))
    same = service.prepare_full_text(question=question, document_ids=[a, b])
    assert same["cache_status"] == "hit"
    result = service.ask_full_text(
        question=question,
        document_ids=[a, b],
        source_versions=same["source_versions"],
        plan_id=same["plan_id"],
    )
    assert result.answer == original.answer and len(model.calls) == 1
    reverse = service.prepare_full_text(question=question, document_ids=[b, a])
    assert reverse["cache_status"] == "miss" and reverse["context_kind"] == "analysis_reuse"
    assert len(model.calls) == 1


def test_reversed_order_cannot_turn_an_old_free_preflight_into_a_paid_call(research_db):
    a, b, question, service, model, _ = warm(research_db)
    free_plan = service.prepare_full_text(question=question, document_ids=[a, b])
    with pytest.raises(FullTextError, match="full_text_plan_changed"):
        service.ask_full_text(
            question=question,
            document_ids=[b, a],
            source_versions=free_plan["source_versions"],
            plan_id=free_plan["plan_id"],
        )
    assert len(model.calls) == 1


def test_new_paper_before_old_pair_reuses_quotes_without_old_numbered_claims(research_db):
    a, b, question, service, model, _ = warm(research_db)
    c = _seed_document(
        research_db,
        content_id="c" * 64,
        display_name="C.pdf",
        page_texts=[["Gamma verified source quotation."]],
    )
    plan = service.prepare_full_text(question=question, document_ids=[c, a, b])
    assert plan["source_modes"] == {c: "full_text", a: "analysis_reuse", b: "analysis_reuse"}
    model.result = _comparison_result(
        [
            ("[d1 p1]", "Gamma verified source quotation."),
            ("[d2 p1]", "Alpha verified source quotation."),
            ("[d3 p1]", "Beta verified source quotation."),
        ],
        "Fresh comparison in the new order",
    )
    service.ask_full_text(
        question=question,
        document_ids=[c, a, b],
        source_versions=plan["source_versions"],
        plan_id=plan["plan_id"],
    )
    payload = json.loads(model.calls[-1])
    assert len(payload["new_documents"]) == 1 and payload["new_documents"][0]["filename"] == "C.pdf"
    assert "Original first-to-second conclusion" not in model.calls[-1]
    assert [(s["ref"], s["quotes"]) for s in payload["sources"]] == [
        ("[d2 p1]", ["Alpha verified source quotation."]),
        ("[d3 p1]", ["Beta verified source quotation."]),
    ]
    assert len(model.calls) == 2
