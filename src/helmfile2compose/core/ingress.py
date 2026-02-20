"""Ingress conversion — IngressProvider abstract class, rewriter dispatch."""

from helmfile2compose.pacts.types import ConvertContext, ConvertResult, Provider
from helmfile2compose.pacts.ingress import IngressRewriter


class _NullRewriter(IngressRewriter):
    """No-op fallback rewriter — returns empty entries."""
    name = "_null"

    def match(self, manifest, ctx):
        return True

    def rewrite(self, manifest, ctx):
        return []


# No built-in rewriters — distributions/extensions populate this
_REWRITERS: list[IngressRewriter] = []


def _is_rewriter_class(obj, mod_name):
    """Check if obj is an ingress rewriter class defined in the given module."""
    return (isinstance(obj, type)
            and hasattr(obj, 'name') and isinstance(getattr(obj, 'name', None), str)
            and hasattr(obj, 'match') and callable(obj.match)
            and hasattr(obj, 'rewrite') and callable(obj.rewrite)
            and not hasattr(obj, 'kinds')
            and obj.__module__ == mod_name)


class IngressProvider(Provider):
    """Abstract ingress provider — rewriter dispatch + service/config generation.

    Subclasses implement build_service() and write_config() to support
    different reverse proxy backends (Caddy, Traefik, etc.).
    """
    name = "ingress"
    kinds = ["Ingress"]
    priority = 90

    def convert(self, _kind: str, manifests: list[dict], ctx: ConvertContext) -> ConvertResult:
        """Convert all Ingress manifests via rewriter dispatch."""
        entries = []
        for m in manifests:
            rewriter = self._find_rewriter(m, ctx)
            entries.extend(rewriter.rewrite(m, ctx))
        services = {}
        if entries and not ctx.config.get("disableCaddy"):
            services = self.build_service(entries, ctx)
        return ConvertResult(services=services, caddy_entries=entries)

    def build_service(self, entries, ctx):
        """Build the reverse proxy compose service dict. Override in subclasses."""
        return {}

    def write_config(self, entries, output_dir, config):
        """Write the reverse proxy config file. Override in subclasses."""

    @staticmethod
    def _find_rewriter(manifest, ctx):
        """Find the first matching rewriter for an Ingress manifest."""
        for rw in _REWRITERS:
            if rw.match(manifest, ctx):
                return rw
        name = manifest.get("metadata", {}).get("name", "?")
        ctx.warnings.append(f"Ingress '{name}': no matching rewriter, skipped")
        return _NullRewriter()
