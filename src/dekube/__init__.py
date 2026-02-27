"""dekube — convert Kubernetes manifests to compose.yml + Caddyfile.

Re-exports the public API for extensions.
Extensions can import directly from here or from dekube.pacts.
"""

from dekube.pacts.types import (
    ConvertContext, ConverterResult, ProviderResult,
    ConvertResult,  # deprecated alias
    Converter, IndexerConverter, Provider,
)
from dekube.pacts.ingress import IngressRewriter, get_ingress_class, resolve_backend
from dekube.pacts.helpers import apply_replacements, _secret_value
from dekube.core.env import resolve_env, _convert_command
from dekube.core.ingress import IngressProvider
from dekube.core.volumes import _convert_volume_mounts, _build_vol_map
from dekube.core.services import (
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
    # K8s-to-compose conversion primitives (pod specs, volumes, ports, commands)
    "_convert_command",
    "_convert_volume_mounts",
    "_build_vol_map",
    "_build_alias_map",
    "_build_service_port_map",
    "_resolve_named_port",
]
