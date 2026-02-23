"""h2c — convert helmfile template output to compose.yml + Caddyfile.

Re-exports the public API for extensions.
Extensions can import directly from here or from h2c.pacts.
"""

from h2c.pacts.types import (
    ConvertContext, ConverterResult, ProviderResult,
    ConvertResult,  # deprecated alias
    Converter, IndexerConverter, Provider,
)
from h2c.pacts.ingress import IngressRewriter, get_ingress_class, resolve_backend
from h2c.pacts.helpers import apply_replacements, _secret_value
from h2c.core.env import resolve_env, _convert_command
from h2c.core.ingress import IngressProvider
from h2c.core.volumes import _convert_volume_mounts, _build_vol_map
from h2c.core.services import (
    _build_alias_map, _build_service_port_map, _resolve_named_port,
)

__all__ = [
    # Types & base classes
    "ConvertContext",
    "ConverterResult",
    "ProviderResult",
    "ConvertResult",  # deprecated alias
    "Converter",
    "IndexerConverter",
    "Provider",
    "IngressRewriter",
    "IngressProvider",
    # Public helpers
    "get_ingress_class",
    "resolve_backend",
    "apply_replacements",
    "resolve_env",
    "_secret_value",
    # Helpers for built-in extensions (workload converter, service indexer)
    "_convert_command",
    "_convert_volume_mounts",
    "_build_vol_map",
    "_build_alias_map",
    "_build_service_port_map",
    "_resolve_named_port",
]
