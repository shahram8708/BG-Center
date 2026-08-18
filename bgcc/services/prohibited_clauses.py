"""Deterministic prohibited-clause engine.

Pure-Python, no AI call, no external dependency. It can only make a deviation
tier MORE severe (Low < High < Prohibited), never less.
"""
import re

from bgcc.models.enums import DeviationTier

_TIER_RANK = {
    DeviationTier.low.value: 0,
    DeviationTier.high.value: 1,
    DeviationTier.prohibited.value: 2,
}


def _normalize(text):
    """Case-insensitive with whitespace/punctuation normalization."""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _rule_re(rule):
    pattern = rule.get("pattern", "")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE)


def scan_text(clause_text, rules):
    """Return the first matching rule (or None) for a clause text.

    Accepts either a single rule dict or a list of rule dicts (the settings
    value shape). Returns a dict of the matched rule if any rule matches.
    """
    if not clause_text:
        return None
    if isinstance(rules, dict):
        rules = [rules]
    if not rules:
        return None
    normalized = _normalize(clause_text)
    for rule in rules:
        if rule is None:
            continue
        pattern = rule.get("pattern", "")
        if not pattern:
            continue
        expr = _rule_re(rule)
        # Match against the normalized text as well as the raw text so
        # formatting differences do not trivially evade detection.
        if expr.search(clause_text) or expr.search(normalized):
            return rule
    return None


def effective_tier(ai_proposed_tier, clause_text, rules):
    """Compute the effective tier as the more severe of AI-proposed and rule verdict."""
    if ai_proposed_tier is not None:
        ai_proposed_tier = str(ai_proposed_tier)
    matched = scan_text(clause_text, rules)
    if matched is not None:
        # Any deterministic match forces Prohibited regardless of the AI verdict.
        return DeviationTier.prohibited.value, matched
    # No rule matched: the AI's own judgment stands.
    if ai_proposed_tier in _TIER_RANK:
        return ai_proposed_tier, None
    return DeviationTier.low.value, None


def rank(tier):
    return _TIER_RANK.get(str(tier), -1)
