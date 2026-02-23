"""Main conversion orchestration — convert(), dispatch, overrides."""

import inspect
import sys

from h2c.pacts.types import (
    ConvertContext, Converter, IndexerConverter, Provider,
)
from h2c.pacts.ingress import IngressRewriter
from h2c.pacts.helpers import _secret_value
from h2c.core.constants import (
    UNSUPPORTED_KINDS, IGNORED_KINDS, _SECRET_REF_RE,
)
from h2c.core.env import _postprocess_env
from h2c.core.services import _build_network_aliases

# No built-in converters — distributions/extensions populate this
_CONVERTERS = []

# Transform instances — post-processing hooks that run after alias injection
_TRANSFORMS = []

# All kinds handled by the converter pipeline (mutable — updated by
# _register_extensions and distribution wiring)
CONVERTED_KINDS = set()


def _deep_merge(base: dict, overrides: dict) -> None:
    """Recursively merge overrides into base. None values delete keys."""
    for key, val in overrides.items():
        if val is None:
            base.pop(key, None)
        elif isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


def _resolve_volume_root(obj, volume_root: str):
    """Recursively resolve $volume_root placeholders in config values."""
    if isinstance(obj, str):
        return obj.replace("$volume_root", volume_root)
    if isinstance(obj, list):
        return [_resolve_volume_root(item, volume_root) for item in obj]
    if isinstance(obj, dict):
        return {k: _resolve_volume_root(v, volume_root) for k, v in obj.items()}
    return obj


def _resolve_secret_refs(obj, secrets: dict, warnings: list[str]):
    """Recursively resolve $secret:<name>:<key> placeholders in config values."""
    if isinstance(obj, str):
        def _replace(m):  # noqa: E301 — closure for re.sub callback
            """Resolve a single $secret:<name>:<key> match."""
            sec_name, sec_key = m.group(1), m.group(2)
            sec = secrets.get(sec_name)
            if sec is None:
                warnings.append(f"$secret ref: Secret '{sec_name}' not found")
                return m.group(0)
            val = _secret_value(sec, sec_key)
            if val is None:
                warnings.append(f"$secret ref: key '{sec_key}' not found in Secret '{sec_name}'")
                return m.group(0)
            return val
        return _SECRET_REF_RE.sub(_replace, obj)
    if isinstance(obj, list):
        return [_resolve_secret_refs(item, secrets, warnings) for item in obj]
    if isinstance(obj, dict):
        return {k: _resolve_secret_refs(v, secrets, warnings) for k, v in obj.items()}
    return obj


def _apply_overrides(compose_services: dict, config: dict,
                     secrets: dict, warnings: list[str]) -> None:
    """Apply service overrides and custom services from config."""
    volume_root = config.get("volume_root", "./data")
    for svc_name, overrides in config.get("overrides", {}).items():
        if svc_name not in compose_services:
            warnings.append(f"override for '{svc_name}' but no such generated service — skipped")
            continue
        resolved = _resolve_secret_refs(overrides, secrets, warnings)
        resolved = _resolve_volume_root(resolved, volume_root)
        _deep_merge(compose_services[svc_name], resolved)
    for svc_name, svc_def in config.get("services", {}).items():
        if svc_name in compose_services:
            warnings.append(f"custom service '{svc_name}' conflicts with generated service — overwritten")
        resolved = _resolve_secret_refs(svc_def, secrets, warnings)
        compose_services[svc_name] = _resolve_volume_root(resolved, volume_root)


def _emit_kind_warnings(manifests: dict, warnings: list[str]) -> None:
    """Emit warnings for unsupported and unknown manifest kinds."""
    for kind in UNSUPPORTED_KINDS:
        for m in manifests.get(kind, []):
            warnings.append(f"{kind} '{m.get('metadata', {}).get('name', '?')}' not supported")
    known = set(CONVERTED_KINDS) | set(UNSUPPORTED_KINDS) | set(IGNORED_KINDS)
    for kind, items in manifests.items():
        if kind not in known:
            warnings.append(f"unknown kind '{kind}' ({len(items)} manifest(s)) — skipped")


def convert(manifests: dict[str, list[dict]], config: dict,
            output_dir: str = ".", first_run: bool = False) -> tuple[dict, list[dict], list[str]]:
    """Main conversion: returns (compose_services, ingress_entries, warnings)."""
    warnings: list[str] = []

    # Build context with empty containers — indexers populate them
    ctx = ConvertContext(
        config=config, output_dir=output_dir,
        replacements=config.get("replacements", []),
        warnings=warnings, manifests=manifests,
        first_run=first_run,
    )

    # Dispatch to converters in priority order
    extensions_config = config.get("extensions", {})
    compose_services: dict = {}
    ingress_entries: list[dict] = []
    for converter in sorted(_CONVERTERS, key=lambda c: getattr(c, 'priority', 1000)):
        ctx.extension_config = extensions_config.get(getattr(converter, 'name', ''), {})
        for kind in converter.kinds:
            result = converter.convert(kind, manifests.get(kind, []), ctx)
            services = getattr(result, 'services', None)
            if services:
                compose_services.update(services)
            ingress_entries.extend(result.ingress_entries)

    # Post-process all services: port remapping and replacements.
    # Idempotent — safe on services already processed by WorkloadConverter.
    _postprocess_env(compose_services, ctx)

    # Add network aliases so K8s FQDNs resolve via compose DNS
    network_aliases = _build_network_aliases(ctx.services_by_selector, ctx.alias_map)
    _inject_network_aliases(compose_services, network_aliases)
    _warn_missing_fqdn(compose_services, network_aliases, ctx.services_by_selector, warnings)

    # PVC volume management: auto-populate on first run, detect stale on subsequent
    config_volumes = config.get("volumes") or {}
    if first_run:
        for pvc in sorted(ctx.pvc_names):
            if pvc not in config_volumes:
                config.setdefault("volumes", {})[pvc] = {"host_path": pvc}
    else:
        for vol_name in sorted(config_volumes):
            if vol_name not in ctx.pvc_names:
                warnings.append(f"volume '{vol_name}' in helmfile2compose.yaml not referenced by any PVC — stale?")

    _emit_kind_warnings(manifests, warnings)
    _apply_overrides(compose_services, config, ctx.secrets, warnings)

    _truncate_hostnames(compose_services)

    # Run transforms (post-processing hooks) after all alias injection
    for transform_cls in _TRANSFORMS:
        transform_cls.transform(compose_services, ingress_entries, ctx)

    return compose_services, ingress_entries, warnings


def _inject_network_aliases(compose_services: dict, network_aliases: dict) -> None:
    """Add compose network aliases so K8s FQDNs resolve via compose DNS."""
    for svc_name, svc_aliases in network_aliases.items():
        if svc_name in compose_services and "network_mode" not in compose_services[svc_name]:
            if svc_aliases:
                compose_services[svc_name]["networks"] = {"default": {"aliases": svc_aliases}}


def _warn_missing_fqdn(compose_services: dict, network_aliases: dict,
                        services_by_selector: dict, warnings: list[str]) -> None:
    """Warn for generated services that have no FQDN aliases (missing namespace)."""
    for svc_name in compose_services:
        if "network_mode" in compose_services[svc_name]:
            continue  # sidecars don't get aliases
        aliases = network_aliases.get(svc_name, [])
        has_fqdn = any(".svc.cluster.local" in a for a in aliases)
        if not has_fqdn and svc_name in services_by_selector:
            warnings.append(
                f"service '{svc_name}' has no FQDN aliases (namespace unknown) — "
                f"other services referencing it by FQDN will fail to resolve. "
                f"Fix your helmfile: add {{{{ .Release.Namespace }}}} to the "
                f"chart's metadata.namespace, or use --helmfile-dir."
            )


def _truncate_hostnames(compose_services: dict) -> None:
    """Set explicit shorter hostname for services >63 chars (Linux hostname limit)."""
    for svc_name, svc in compose_services.items():
        if len(svc_name) > 63 and "hostname" not in svc:
            svc["hostname"] = svc_name[:63]


# Base classes to skip during auto-registration
_BASE_CLASSES = (Converter, IndexerConverter, Provider, IngressRewriter)


def _auto_register() -> None:
    """Scan the caller's globals for converter/rewriter/transform classes and register them.

    Called by build-distribution.py after all extension code has been concatenated.
    Populates _CONVERTERS, _TRANSFORMS, _REWRITERS (from core.ingress), and CONVERTED_KINDS.
    Crashes on duplicate kind claims.
    """
    from h2c.core.ingress import _REWRITERS, _is_rewriter_class, IngressProvider
    from h2c.core.extensions import _is_converter_class, _is_transform_class

    skip = _BASE_CLASSES + (IngressProvider,)
    caller_globals = inspect.stack()[1][0].f_globals
    converters = []
    transforms = []
    rewriters = []
    for name, obj in caller_globals.items():
        if not isinstance(obj, type) or obj in skip or name.startswith("_"):
            continue
        # Use __main__ or whatever the caller's module name is
        mod_name = obj.__module__
        if _is_converter_class(obj, mod_name):
            converters.append(obj())
        elif _is_rewriter_class(obj, mod_name):
            rewriters.append(obj())
        elif _is_transform_class(obj, mod_name):
            transforms.append(obj())

    # Check for duplicate kind claims
    kind_owners: dict[str, str] = {}
    for c in converters:
        for k in c.kinds:
            if k in kind_owners:
                print(f"Error: kind '{k}' claimed by both "
                      f"{kind_owners[k]} and {type(c).__name__}",
                      file=sys.stderr)
                sys.exit(1)
            kind_owners[k] = type(c).__name__

    # Sort by priority and register
    converters.sort(key=lambda c: getattr(c, 'priority', 1000))
    transforms.sort(key=lambda t: getattr(t, 'priority', 1000))
    rewriters.sort(key=lambda r: getattr(r, 'priority', 1000))
    _CONVERTERS.extend(converters)
    _TRANSFORMS.extend(transforms)
    _REWRITERS.extend(rewriters)
    CONVERTED_KINDS.update(k for c in converters for k in c.kinds)
