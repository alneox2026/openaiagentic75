"""Shared identifiers and validation helpers for customer billing records."""

from __future__ import annotations

from hashlib import sha256
from typing import Any


def customer_wallet_document_id(billing_subject_id: str) -> str:
    """Return a stable opaque wallet document id for a billing subject."""

    return f"wallet-{_subject_digest(billing_subject_id)}"


def customer_billing_period_document_id(
    billing_subject_id: str,
    period_key: str,
) -> str:
    """Return a stable opaque monthly-period document id."""

    return f"period-{_subject_digest(billing_subject_id)}-{period_key}"




def nonnegative_int(value: Any, *, field_name: str) -> int:
    """Read a Firestore integer field without accepting booleans or negatives."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _subject_digest(billing_subject_id: str) -> str:
    normalized = billing_subject_id.strip()
    if not normalized:
        raise ValueError("billing_subject_id must not be empty.")
    return sha256(normalized.encode("utf-8")).hexdigest()
