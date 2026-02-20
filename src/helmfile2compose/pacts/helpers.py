"""Public helper functions available to extensions."""

import base64


def apply_replacements(text: str, replacements: list[dict]) -> str:
    """Apply user-defined string replacements from config."""
    for r in replacements:
        text = text.replace(r["old"], r["new"])
    return text


def _secret_value(secret: dict, key: str) -> str | None:
    """Get a decoded value from a K8s Secret (base64 data or plain stringData)."""
    # stringData is plain text (rare in rendered output, but possible)
    val = (secret.get("stringData") or {}).get(key)
    if val is not None:
        return val
    # data is base64-encoded
    val = (secret.get("data") or {}).get(key)
    if val is not None:
        try:
            return base64.b64decode(val).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return val  # fallback: return raw if decode fails
    return None
