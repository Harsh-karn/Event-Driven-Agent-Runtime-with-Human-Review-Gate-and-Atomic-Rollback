"""Event telemetry and PII/Secret redaction engine for Tether warehouse export."""

import re
from typing import Any

# Regex patterns matching sensitive credentials, tokens, and PII
SECRET_PATTERNS = [
    # OpenAI, Anthropic, GitHub, Google API Keys
    (re.compile(r"sk-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{30,}", re.IGNORECASE), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}", re.IGNORECASE), "[REDACTED_GOOGLE_API_KEY]"),
    (re.compile(r"xox[baprs]-[0-9a-zA-Z_\-]+", re.IGNORECASE), "[REDACTED_SLACK_TOKEN]"),
    # Bearer Authentication tokens
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\._\-]{20,}", re.IGNORECASE), r"\1[REDACTED_BEARER_TOKEN]"),
    # Database connection strings with passwords
    (
        re.compile(r"(postgres(?:ql)?://[^:]+:)([^@]+)(@)", re.IGNORECASE),
        r"\1[REDACTED_PASSWORD]\3",
    ),
    # Credit Card Numbers (Luhn-style 16 digit groups)
    (
        re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b"),
        "[REDACTED_CREDIT_CARD]",
    ),
]

SENSITIVE_KEY_NAMES = {
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "cookie",
    "set-cookie",
}


def sanitize_string(value: str) -> str:
    """Scrub sensitive patterns from text strings."""
    cleaned = value
    for pattern, replacement in SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def sanitize_payload(obj: Any) -> Any:
    """Recursively sanitize dicts, lists, and primitive data types prior to warehouse ingestion."""
    if isinstance(obj, dict):
        sanitized: dict[str, Any] = {}
        for k, v in obj.items():
            key_lower = str(k).lower().strip()
            if any(sens in key_lower for sens in SENSITIVE_KEY_NAMES):
                sanitized[k] = "[REDACTED_SECRET]"
            else:
                sanitized[k] = sanitize_payload(v)
        return sanitized

    elif isinstance(obj, list):
        return [sanitize_payload(item) for item in obj]

    elif isinstance(obj, str):
        return sanitize_string(obj)

    return obj
