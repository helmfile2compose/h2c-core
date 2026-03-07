"""Public contracts for extensions — the sacred pacts."""

from dekube.pacts.types import (
    ConvertContext, ConverterResult, ProviderResult,
    ConvertResult,  # deprecated alias
    Converter, IndexerConverter, Provider,
)
from dekube.pacts.ingress import IngressRewriter, get_ingress_class, resolve_backend
from dekube.pacts.helpers import apply_replacements, secret_value
from dekube.core.env import resolve_env

# Backward compat alias (deprecated — use secret_value)
_secret_value = secret_value

__all__ = [
    "ConvertContext",
    "ConverterResult",
    "ProviderResult",
    "ConvertResult",
    "Converter",
    "IndexerConverter",
    "Provider",
    "IngressRewriter",
    "get_ingress_class",
    "resolve_backend",
    "apply_replacements",
    "resolve_env",
    "secret_value",
    "_secret_value",
]
