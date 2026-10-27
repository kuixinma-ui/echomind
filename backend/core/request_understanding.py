"""Validate request semantics and conservatively reconcile existing intent labels.

This module deliberately uses raw labels and only standard-library dependencies,
so classification and diagnostic consumers can share it without import cycles.
"""

import math


RESOLUTION_REASONS = frozenset({
    "legacy_vote",
    "semantic_agreement",
    "policy_domain",
    "human_request",
    "human_subtype",
    "critical_guard",
    "domain_conflict",
    "insufficient_semantic_confidence",
})

_FIELDS = frozenset({"version", "purpose", "domain", "confidence"})
_PURPOSES = frozenset({"policy_question", "service_action", "human_handoff", "unclear"})
_DOMAINS = frozenset({"general", "technical", "billing", "account", "order_logistics", "unknown"})
_POLICY_TARGETS = {
    "general": "query",
    "technical": "technical",
    "billing": "billing",
    "account": "account",
    "order_logistics": "query",
}
_CORRECTABLE = frozenset({"other", "query", "technical", "billing", "account", "human_handoff", "escalation"})
_SPECIFIC_DOMAINS = {
    "technical_login": "technical",
    "technical_crash": "technical",
    "refund": "billing",
    "invoice": "billing",
    "payment_issue": "billing",
    "account_security": "account",
    "order_status": "order_logistics",
    "logistics": "order_logistics",
}


def _valid_confidence(value) -> bool:
    """Accept JSON real numbers; reject booleans and non-JSON numeric objects."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 1
        and math.isfinite(value)
    )


def normalize_understanding(value) -> dict:
    """Return a fresh, exact version-1 contract, or a fresh empty dictionary."""
    if not isinstance(value, dict) or value.keys() != _FIELDS:
        return {}
    if type(value["version"]) is not int or value["version"] != 1:
        return {}
    if not isinstance(value["purpose"], str) or value["purpose"] not in _PURPOSES:
        return {}
    if not isinstance(value["domain"], str) or value["domain"] not in _DOMAINS:
        return {}
    if not _valid_confidence(value["confidence"]):
        return {}
    return dict(value)


def resolve_intent(
    voted_intent: str,
    understanding: dict,
    *,
    llm_confidence,
    knowledge_required: bool,
    critical: bool,
    threshold: float,
    llm_intent: str = "",
) -> tuple[str, str]:
    """Return the reconciled existing label and a fixed diagnostic reason.

    Critical requests retain their original vote before any semantic validation.
    Confidence remains separate from the existing classification vote evidence.
    """
    if critical:
        return voted_intent, "critical_guard"
    normalized = normalize_understanding(understanding)
    if not normalized:
        return voted_intent, "legacy_vote"
    if (
        not _valid_confidence(llm_confidence)
        or not _valid_confidence(threshold)
        or llm_confidence < threshold
        or normalized["confidence"] < threshold
    ):
        return voted_intent, "insufficient_semantic_confidence"

    purpose = normalized["purpose"]
    if purpose == "human_handoff":
        if isinstance(llm_intent, str) and llm_intent in {"human_handoff", "escalation"}:
            return llm_intent, "semantic_agreement" if voted_intent == llm_intent else "human_subtype"
        if voted_intent in {"human_handoff", "escalation"}:
            return voted_intent, "semantic_agreement"
        return "human_handoff", "human_request"

    domain = normalized["domain"]
    if purpose != "policy_question" or knowledge_required is not True or domain == "unknown":
        return voted_intent, "legacy_vote"

    if _SPECIFIC_DOMAINS.get(voted_intent) == domain:
        return voted_intent, "semantic_agreement"
    if voted_intent not in _CORRECTABLE:
        return voted_intent, "domain_conflict"
    target = _POLICY_TARGETS[domain]
    return target, "semantic_agreement" if voted_intent == target else "policy_domain"
