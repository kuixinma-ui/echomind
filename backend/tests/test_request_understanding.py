"""Pure contract and intent reconciliation boundaries; no service dependencies."""

from importlib import import_module
from decimal import Decimal
from fractions import Fraction

import pytest


def contract():
    return import_module("core.request_understanding")


def understanding(**changes):
    return {
        "version": 1,
        "purpose": "policy_question",
        "domain": "general",
        "confidence": 0.9,
        **changes,
    }


def resolve(voted="other", value=None, **changes):
    options = {
        "llm_confidence": 0.9,
        "knowledge_required": True,
        "critical": False,
        "threshold": 0.5,
        **changes,
    }
    return contract().resolve_intent(
        voted, understanding() if value is None else value, **options
    )


@pytest.mark.parametrize("purpose", ["policy_question", "service_action", "human_handoff", "unclear"])
@pytest.mark.parametrize("domain", ["general", "technical", "billing", "account", "order_logistics", "unknown"])
def test_normalization_returns_fresh_valid_contract(purpose, domain):
    original = understanding(purpose=purpose, domain=domain)
    actual = contract().normalize_understanding(original)
    assert actual == original
    assert actual is not original
    actual["purpose"] = "unclear"
    assert original["purpose"] == purpose


@pytest.mark.parametrize("value", [None, [], "{}", 1, True, {}, {"version": 1}])
def test_normalization_rejects_non_contracts(value):
    assert contract().normalize_understanding(value) == {}


@pytest.mark.parametrize("field", ["version", "purpose", "domain", "confidence"])
def test_normalization_requires_every_field(field):
    value = understanding()
    del value[field]
    assert contract().normalize_understanding(value) == {}


def test_normalization_rejects_extra_fields_and_returns_fresh_empty_dicts():
    first = contract().normalize_understanding(understanding(reasoning="untrusted"))
    second = contract().normalize_understanding(understanding(reasoning="untrusted"))
    assert first == second == {}
    assert first is not second


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 0, 2, None, []])
def test_normalization_requires_integer_version_one(version):
    assert contract().normalize_understanding(understanding(version=version)) == {}


@pytest.mark.parametrize("field", ["purpose", "domain"])
@pytest.mark.parametrize("value", [None, True, 1, [], {}, "POLICY_QUESTION", "unsupported", ""])
def test_normalization_rejects_unknown_or_non_string_enums(field, value):
    assert contract().normalize_understanding(understanding(**{field: value})) == {}


INVALID_CONFIDENCES = [None, True, False, "0.9", [], {}, -0.01, 1.01, float("nan"), float("inf"), -float("inf"), complex(0.9, 0), Decimal("0.9"), Fraction(1, 2)]


@pytest.mark.parametrize("confidence", INVALID_CONFIDENCES)
def test_normalization_rejects_invalid_confidence(confidence):
    assert contract().normalize_understanding(understanding(confidence=confidence)) == {}


@pytest.mark.parametrize("confidence", [0, 1, 0.0, 1.0, 0.5])
def test_normalization_accepts_confidence_endpoints(confidence):
    assert contract().normalize_understanding(understanding(confidence=confidence)) == understanding(confidence=confidence)


@pytest.mark.parametrize("value", [{}, {"version": 1}, understanding(confidence=True)])
def test_invalid_or_legacy_contract_preserves_vote(value):
    assert resolve("human_handoff", value) == ("human_handoff", "legacy_vote")


@pytest.mark.parametrize("field", ["confidence", "llm_confidence"])
@pytest.mark.parametrize("confidence", INVALID_CONFIDENCES + [0.49])
def test_both_confidences_are_required(field, confidence):
    value = understanding(**({field: confidence} if field == "confidence" else {}))
    options = {field: confidence} if field == "llm_confidence" else {}
    expected_reason = "legacy_vote" if field == "confidence" and confidence != 0.49 else "insufficient_semantic_confidence"
    assert resolve("human_handoff", value, **options) == ("human_handoff", expected_reason)


def test_joint_confidence_uses_supplied_threshold_inclusively():
    assert resolve(value=understanding(confidence=0.7), llm_confidence=0.7, threshold=0.7) == ("query", "policy_domain")
    assert resolve(value=understanding(confidence=0.69), threshold=0.7) == ("other", "insufficient_semantic_confidence")
    assert resolve(llm_confidence=0.69, threshold=0.7) == ("other", "insufficient_semantic_confidence")


@pytest.mark.parametrize("value", [{}, understanding(), understanding(purpose="human_handoff"), understanding(confidence=True)])
def test_critical_guard_precedes_contract_and_confidence(value):
    assert resolve("other", value, critical=True, llm_confidence=False) == ("other", "critical_guard")


@pytest.mark.parametrize("domain, target", [("general", "query"), ("technical", "technical"), ("billing", "billing"), ("account", "account"), ("order_logistics", "query")])
@pytest.mark.parametrize("voted", ["other", "query", "technical", "billing", "account", "human_handoff", "escalation"])
def test_policy_maps_only_broad_and_handoff_votes(domain, target, voted):
    assert resolve(voted, understanding(domain=domain)) == (target, "semantic_agreement" if voted == target else "policy_domain")


@pytest.mark.parametrize("domain, voted", [("technical", "technical_login"), ("technical", "technical_crash"), ("billing", "refund"), ("billing", "invoice"), ("billing", "payment_issue"), ("account", "account_security"), ("order_logistics", "order_status"), ("order_logistics", "logistics")])
def test_policy_preserves_same_domain_specific_label(domain, voted):
    assert resolve(voted, understanding(domain=domain)) == (voted, "semantic_agreement")


@pytest.mark.parametrize("domain, voted", [("general", "order_status"), ("general", "request"), ("general", "complaint"), ("general", "greeting"), ("general", "feedback"), ("technical", "refund"), ("billing", "technical_login"), ("account", "invoice"), ("order_logistics", "account_security")])
def test_policy_preserves_unrelated_specifics_actions_and_complaints(domain, voted):
    assert resolve(voted, understanding(domain=domain)) == (voted, "domain_conflict")


@pytest.mark.parametrize("knowledge", [False, None, 1, "true", [], {}])
def test_policy_requires_literal_knowledge_true(knowledge):
    assert resolve("human_handoff", knowledge_required=knowledge) == ("human_handoff", "legacy_vote")


@pytest.mark.parametrize("purpose, domain", [("service_action", "technical"), ("unclear", "general"), ("policy_question", "unknown")])
def test_non_policy_or_unknown_domain_preserves_legacy(purpose, domain):
    assert resolve("query", understanding(purpose=purpose, domain=domain)) == ("query", "legacy_vote")


@pytest.mark.parametrize("voted, expected", [("other", ("human_handoff", "human_request")), ("refund", ("human_handoff", "human_request")), ("human_handoff", ("human_handoff", "semantic_agreement")), ("escalation", ("escalation", "semantic_agreement"))])
@pytest.mark.parametrize("domain", ["general", "unknown"])
def test_actual_human_request_does_not_require_knowledge_or_known_domain(voted, expected, domain):
    assert resolve(voted, understanding(purpose="human_handoff", domain=domain), knowledge_required=False) == expected


def test_contract_resolution_does_not_mutate_input():
    value = understanding(domain="technical")
    assert resolve("query", value) == ("technical", "policy_domain")
    assert value == {"version": 1, "purpose": "policy_question", "domain": "technical", "confidence": 0.9}


def test_resolution_reasons_can_sanitize_diagnostics():
    assert isinstance(contract().RESOLUTION_REASONS, frozenset)
    assert "policy_domain" in contract().RESOLUTION_REASONS
    assert "freeform reasoning" not in contract().RESOLUTION_REASONS
