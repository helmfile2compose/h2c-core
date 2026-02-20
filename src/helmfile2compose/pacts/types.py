"""Public data types for extensions — the sacred contracts."""

from dataclasses import dataclass, field

# Well-known named ports (used by resolve_backend in pacts/ingress.py)
WELL_KNOWN_PORTS = {"http": 80, "https": 443, "grpc": 50051}


@dataclass
class ConvertContext:
    """Shared state passed to all converters during a conversion run."""
    config: dict
    output_dir: str
    configmaps: dict = field(default_factory=dict)
    secrets: dict = field(default_factory=dict)
    services_by_selector: dict = field(default_factory=dict)
    alias_map: dict = field(default_factory=dict)
    service_port_map: dict = field(default_factory=dict)
    replacements: list = field(default_factory=list)
    pvc_names: set = field(default_factory=set)
    warnings: list = field(default_factory=list)
    generated_cms: set = field(default_factory=set)
    generated_secrets: set = field(default_factory=set)
    fix_permissions: dict = field(default_factory=dict)
    manifests: dict = field(default_factory=dict)
    extension_config: dict = field(default_factory=dict)


@dataclass
class ConvertResult:
    """Output of a single converter."""
    services: dict = field(default_factory=dict)
    caddy_entries: list = field(default_factory=list)


class Converter:
    """Base class for all converters — indexers, providers, and custom extensions."""
    name: str = ""
    kinds: list = []
    priority: int = 1000

    def convert(self, kind, manifests, ctx):
        """Convert manifests of a given kind. Override in subclasses."""
        return ConvertResult()


class IndexerConverter(Converter):
    """Converter that populates ConvertContext fields (returns empty ConvertResult)."""
    priority: int = 50


class Provider(Converter):
    """Converter that produces compose services in ConvertResult."""
    priority: int = 500
