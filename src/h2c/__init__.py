"""h2c — convert helmfile template output to compose.yml + Caddyfile.

Re-exports the public API for extensions.
Extensions can import directly from here or from h2c.pacts.
"""

from h2c.pacts.types import (
    ConvertContext, ConvertResult, Converter, IndexerConverter, Provider,
)
from h2c.pacts.ingress import IngressRewriter, get_ingress_class, resolve_backend
from h2c.pacts.helpers import apply_replacements, _secret_value
from h2c.core.env import resolve_env

__all__ = [
    "ConvertContext",
    "ConvertResult",
    "Converter",
    "IndexerConverter",
    "Provider",
    "IngressRewriter",
    "get_ingress_class",
    "resolve_backend",
    "apply_replacements",
    "resolve_env",
    "_secret_value",
]
