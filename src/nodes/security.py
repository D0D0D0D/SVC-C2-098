"""Domain security helpers shared by S-2/S-3 hooks and workflow nodes."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from shared.security.pii_detector import detect_pii


SAFE_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,128}$")
SENSITIVE_OPERATION_RE = re.compile(
    r"(?:\b(?:password|secret|token|api[_ -]?key|hostname|ip address)\b|"
    r"(?:パスワード|認証情報|内部(?:URL|アドレス)|監視(?:値|ログ)原文))",
    re.IGNORECASE,
)
FORBIDDEN_OUTPUT_KEYS = frozenset({"rca_accepted", "penalty_amount", "sla_credit", "contract_remedy"})
FORBIDDEN_DECISION_TERMS = (
    "rca accepted",
    "rca is accepted",
    "penalty amount",
    "sla credit",
    "contract remedy",
    "rca受理",
    "ペナルティ算定",
    "slaクレジット",
    "契約是正",
)


def normalized_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def normalized_guard_token(value: Any) -> str:
    """Normalize guard keys/terms across case, width, space, and separators."""

    return re.sub(r"[_\-\s]+", "", normalized_text(value))


_FORBIDDEN_GUARD_KEYS = frozenset(normalized_guard_token(value) for value in FORBIDDEN_OUTPUT_KEYS)
_FORBIDDEN_GUARD_TERMS = tuple(normalized_guard_token(value) for value in FORBIDDEN_DECISION_TERMS)


def contains_forbidden_decision(value: Any) -> bool:
    if isinstance(value, dict):
        return any(contains_forbidden_decision(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_forbidden_decision(item) for item in value)
    token = normalized_guard_token(value)
    return any(term in token for term in _FORBIDDEN_GUARD_TERMS)


def find_forbidden_keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if normalized_guard_token(key) in _FORBIDDEN_GUARD_KEYS:
                found.append(str(key))
            found.extend(find_forbidden_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(find_forbidden_keys(child))
    return found


def has_pii_or_sensitive_operation(value: Any) -> bool:
    # PII detection relies solely on the framework-standard ``detect_pii``.
    # A bespoke Japanese-name supplement was removed (SVC-C2-098 B-5): a
    # honorific/context dictionary produced too many misses to be worth the
    # maintenance burden and over-masking.  Root-fixing the framework's
    # Japanese-name coverage belongs to the framework detector, not to this template.
    if isinstance(value, dict):
        return any(has_pii_or_sensitive_operation(item) for item in value.values())
    if isinstance(value, list):
        return any(has_pii_or_sensitive_operation(item) for item in value)
    if not isinstance(value, str):
        return False
    return bool(detect_pii(value) or SENSITIVE_OPERATION_RE.search(value))


def require_safe_ids(values: Iterable[Any]) -> list[str]:
    result = [str(value) for value in values]
    if any(not SAFE_ID_RE.fullmatch(value) for value in result):
        raise ValueError("unsafe_identifier")
    return result
