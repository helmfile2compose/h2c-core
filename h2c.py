#!/usr/bin/env python3
"""h2c — bare conversion engine (no built-in extensions)."""
# pylint: disable=too-many-locals

import re
from dataclasses import dataclass, field
import base64
import os
import importlib.util
import sys
from pathlib import Path
import inspect
import json
import shutil
import subprocess
import argparse

import yaml

sys.modules.setdefault('h2c', sys.modules[__name__])


# --- core.constants ---


# Workload name patterns auto-excluded on first run (K8s-only infra)
AUTO_EXCLUDE_PATTERNS = ("cert-manager", "ingress", "reflector")

# K8s internal DNS → compose service name
_K8S_DNS_RE = re.compile(
    r'([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.'       # service name (captured)
    r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.'       # namespace (discarded)
    r'svc(?:\.cluster\.local)?'                    # svc[.cluster.local]
)

# Placeholder for referencing secrets in overrides/custom services: $secret:<name>:<key>
_SECRET_REF_RE = re.compile(r'\$secret:([^:]+):([^:}\s]+)')

# K8s kinds we warn about (not convertible to compose)
UNSUPPORTED_KINDS = (
    "CronJob", "HorizontalPodAutoscaler", "PodDisruptionBudget",
)

# K8s kinds silently ignored (no compose equivalent, no useful warning)
IGNORED_KINDS = (
    "Certificate", "ClusterIssuer", "Issuer",
    "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding",
    "CustomResourceDefinition", "IngressClass", "Namespace",
    "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration",
    "NetworkPolicy", "ServiceAccount",
)

# K8s kinds that produce compose services (iterated together everywhere)
WORKLOAD_KINDS = ("DaemonSet", "Deployment", "Job", "StatefulSet")

# K8s $(VAR) interpolation in command/args (kubelet resolves these from env vars)
_K8S_VAR_REF_RE = re.compile(r'\$\(([A-Za-z_][A-Za-z0-9_]*)\)')

# Regex boundary for URL port rewriting (matches end-of-string or path/whitespace/quote)
_URL_BOUNDARY = r'''(?=[/\s"']|$)'''

# --- pacts.types ---


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
    first_run: bool = False


@dataclass
class ConverterResult:
    """Output of a Converter/IndexerConverter — no services."""
    ingress_entries: list = field(default_factory=list)


@dataclass
class ProviderResult(ConverterResult):
    """Output of a Provider — with services."""
    services: dict = field(default_factory=dict)


# Deprecated alias — backwards compat for third-party extensions
ConvertResult = ProviderResult


class Converter:
    """Base class for all converters — indexers, providers, and custom extensions."""
    name: str = ""
    kinds: tuple = ()
    priority: int = 1000

    def convert(self, kind, manifests, ctx):
        """Convert manifests of a given kind. Override in subclasses."""
        return ConverterResult()


class IndexerConverter(Converter):
    """Converter that populates ConvertContext fields (returns empty ConverterResult)."""
    priority: int = 50


class Provider(Converter):
    """Converter that produces compose services in ProviderResult."""
    priority: int = 500

# --- pacts.helpers ---



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

# --- pacts.ingress ---



class IngressRewriter:
    """Base class for ingress annotation rewriters.

    Subclass to support a specific ingress controller. Each rewriter
    translates controller-specific annotations into Caddy entries.
    """
    name: str = ""
    priority: int = 1000

    def match(self, manifest: dict, ctx: ConvertContext) -> bool:
        """Return True if this rewriter handles this Ingress manifest."""
        return False

    def rewrite(self, manifest: dict, ctx: ConvertContext) -> list[dict]:
        """Convert one Ingress manifest to Caddy entries.

        Each entry dict must have: host, path, upstream, scheme.
        Optional: server_ca_secret, server_sni, strip_prefix, extra_directives.
        extra_directives is a list of raw Caddy directive strings.
        """
        return []


def get_ingress_class(manifest: dict,
                      ingress_types: dict[str, str] | None = None) -> str:
    """Extract the ingress class from a manifest (spec or annotation).

    If *ingress_types* is provided, custom class names are resolved to
    canonical rewriter names (e.g. ``haproxy-internal`` → ``haproxy``).
    """
    spec = manifest.get("spec") or {}
    cls = spec.get("ingressClassName", "")
    if not cls:
        cls = ((manifest.get("metadata") or {}).get("annotations") or {}).get(
            "kubernetes.io/ingress.class", "")
    cls = cls.lower()
    if ingress_types and cls in ingress_types:
        cls = ingress_types[cls].lower()
    return cls


def resolve_backend(path_entry: dict, manifest: dict,
                    ctx: ConvertContext) -> dict:
    """Resolve an Ingress path entry to upstream components.

    Returns a dict with: svc_name, compose_name, container_port,
    upstream (host:port string), ns.
    Handles both v1 and v1beta1 Ingress backend formats.
    """
    ns = manifest.get("metadata", {}).get("namespace", "")
    backend = path_entry.get("backend", {})
    if "service" in backend:
        svc_name = backend["service"].get("name", "")
        port = backend["service"].get("port", {})
        svc_port = port.get("number", port.get("name", 80))
    else:
        svc_name = backend.get("serviceName", "")
        svc_port = backend.get("servicePort", 80)

    compose_name = ctx.alias_map.get(svc_name, svc_name)
    container_port = ctx.service_port_map.get(
        (svc_name, svc_port), svc_port)
    # Resolve well-known named ports that survived the lookup
    if isinstance(container_port, str):
        resolved = WELL_KNOWN_PORTS.get(container_port)
        if resolved is not None:
            container_port = resolved
        else:
            ctx.warnings.append(
                f"Ingress backend {svc_name}: unresolved named port '{container_port}'")
            container_port = 80

    svc_ns = ctx.services_by_selector.get(
        svc_name, {}).get("namespace", "") or ns
    if svc_ns:
        upstream_host = f"{svc_name}.{svc_ns}.svc.cluster.local"
    else:
        upstream_host = compose_name

    return {
        "svc_name": svc_name,
        "compose_name": compose_name,
        "container_port": container_port,
        "upstream": f"{upstream_host}:{container_port}",
        "ns": svc_ns or ns,
    }

# --- core.env ---




def _apply_port_remap(text: str, service_port_map: dict) -> str:
    """Rewrite URLs to use container ports instead of K8s Service ports.

    In K8s, Services remap ports (e.g., Service port 80 → container port 8080).
    Compose has no service layer, so URLs must use the actual container port.
    """
    # Group by service name, skip identity mappings and named ports
    remaps: dict[str, list[tuple[int, int]]] = {}
    for (svc_name, svc_port), container_port in service_port_map.items():
        if not isinstance(svc_port, int) or svc_port == container_port:
            continue
        remaps.setdefault(svc_name, []).append((svc_port, container_port))

    for svc_name, port_pairs in remaps.items():
        escaped = re.escape(svc_name)
        for svc_port, container_port in port_pairs:
            # Explicit port: ://host:svc_port or @host:svc_port
            text = re.sub(
                r'(?<=[/@])' + escaped + ':' + str(svc_port) + _URL_BOUNDARY,
                f'{svc_name}:{container_port}',
                text,
            )
            # Implicit port: http://host (80) or https://host (443)
            if svc_port == 80:
                text = re.sub(
                    r'(http://)' + escaped + _URL_BOUNDARY,
                    r'\g<1>' + f'{svc_name}:{container_port}',
                    text,
                )
            elif svc_port == 443:
                text = re.sub(
                    r'(https://)' + escaped + _URL_BOUNDARY,
                    r'\g<1>' + f'{svc_name}:{container_port}',
                    text,
                )

    return text


def _apply_alias_map(text: str, alias_map: dict[str, str]) -> str:
    """Replace K8s Service names with compose service names in hostname positions.

    Matches aliases preceded by :// or @ (URLs, Redis URIs) and followed by
    / : whitespace, quotes, or end-of-string — so only hostnames are affected,
    not substrings like bucket names.
    """
    for alias, target in alias_map.items():
        text = re.sub(
            r'(?<=[/@])'          # preceded by / (in ://) or @
            + re.escape(alias)
            + r'''(?=[/:\s"']|$)''',  # followed by / : whitespace quotes or end
            target,
            text,
        )
    return text


def _resolve_k8s_var_refs(obj, env_dict: dict[str, str]):
    """Replace K8s $(VAR_NAME) references with actual env var values.

    Kubelet resolves $(VAR) in command/args from the container's env vars.
    Compose doesn't do this, so we inline the values at generation time.
    """
    if isinstance(obj, str):
        return _K8S_VAR_REF_RE.sub(lambda m: env_dict.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, list):
        return [_resolve_k8s_var_refs(item, env_dict) for item in obj]
    return obj


def _escape_shell_vars_for_compose(obj):
    """Escape $VAR references in command/entrypoint so compose doesn't interpolate them.

    Compose treats $VAR and ${VAR} as variable substitution from host env / .env file.
    Container commands that use shell $VAR expansion need $$ escaping in compose YAML.
    """
    if isinstance(obj, str):
        return re.sub(r'\$(?=[A-Za-z_{])', '$$', obj)
    if isinstance(obj, list):
        return [_escape_shell_vars_for_compose(item) for item in obj]
    return obj


def _resolve_env_entry(entry: dict, configmaps: dict, secrets: dict,
                       workload_name: str, warnings: list[str]) -> dict | None:
    """Resolve a single K8s env entry (value, configMapKeyRef, or secretKeyRef)."""
    name = entry.get("name", "")
    if "value" in entry:
        return {"name": name, "value": entry["value"]}
    if "valueFrom" not in entry:
        return None
    vf = entry["valueFrom"]
    if "configMapKeyRef" in vf:
        ref = vf["configMapKeyRef"]
        val = (configmaps.get(ref.get("name", ""), {}).get("data") or {}).get(ref.get("key", ""))
        if val is not None:
            return {"name": name, "value": val}
        warnings.append(
            f"configMapKeyRef '{ref.get('name')}/{ref.get('key')}' "
            f"on {workload_name} could not be resolved"
        )
    elif "secretKeyRef" in vf:
        ref = vf["secretKeyRef"]
        val = _secret_value(secrets.get(ref.get("name", ""), {}), ref.get("key", ""))
        if val is not None:
            return {"name": name, "value": val}
        warnings.append(
            f"secretKeyRef '{ref.get('name')}/{ref.get('key')}' "
            f"on {workload_name} could not be resolved"
        )
    elif "fieldRef" in vf:
        field_path = vf["fieldRef"].get("fieldPath", "")
        if field_path == "status.podIP":
            # In compose, the service name is the container's DNS address.
            svc_name = workload_name.split("/", 1)[-1] if "/" in workload_name else workload_name
            return {"name": name, "value": svc_name}
        warnings.append(
            f"env var '{name}' on {workload_name} uses unsupported fieldRef '{field_path}' — skipped"
        )
    else:
        warnings.append(
            f"env var '{name}' on {workload_name} uses unsupported valueFrom — skipped"
        )
    return None


def _resolve_envfrom(envfrom_list: list, configmaps: dict, secrets: dict) -> list[dict]:
    """Resolve envFrom entries (configMapRef, secretRef) into flat env vars."""
    env_vars: list[dict] = []
    for ef in envfrom_list:
        if "configMapRef" in ef:
            cm = configmaps.get(ef["configMapRef"].get("name", ""), {})
            for k, v in (cm.get("data") or {}).items():
                env_vars.append({"name": k, "value": v})
        elif "secretRef" in ef:
            sec = secrets.get(ef["secretRef"].get("name", ""), {})
            for k in sec.get("data") or {}:
                val = _secret_value(sec, k)
                if val is not None:
                    env_vars.append({"name": k, "value": val})
    return env_vars


def _postprocess_env(services: dict, ctx) -> None:
    """Apply port remapping and replacements to all services.

    Providers that build services from scratch may not apply port remapping or
    user-defined replacements to their env vars. This pass catches them.
    Safe to run on already-processed services (idempotent).
    """
    for _svc_name, svc in services.items():
        env = svc.get("environment")
        if not env or not isinstance(env, dict):
            continue
        for key in list(env):
            val = env[key]
            if not isinstance(val, str):
                continue
            original = val
            if ctx.service_port_map:
                val = _apply_port_remap(val, ctx.service_port_map)
            if ctx.replacements:
                val = apply_replacements(val, ctx.replacements)
            if val != original:
                env[key] = val


def _rewrite_env_values(env_vars: list[dict],
                        replacements: list[dict] | None = None,
                        service_port_map: dict | None = None) -> None:
    """Apply port remapping and replacements to env values."""
    # Apply transforms: port remap → user replacements
    transforms = []
    if service_port_map:
        transforms.append(lambda v: _apply_port_remap(v, service_port_map))
    if replacements:
        transforms.append(lambda v: apply_replacements(v, replacements))
    for ev in env_vars:
        if ev["value"] is not None and isinstance(ev["value"], str):
            for transform in transforms:
                ev["value"] = transform(ev["value"])


def resolve_env(container: dict, configmaps: dict[str, dict], secrets: dict[str, dict],
                workload_name: str, warnings: list[str],
                replacements: list[dict] | None = None,
                service_port_map: dict | None = None) -> list[dict]:
    """Resolve env and envFrom into a flat list of {name: ..., value: ...}."""
    env_vars: list[dict] = []

    for e in (container.get("env") or []):
        resolved = _resolve_env_entry(e, configmaps, secrets, workload_name, warnings)
        if resolved:
            env_vars.append(resolved)

    env_vars.extend(_resolve_envfrom(container.get("envFrom") or [], configmaps, secrets))

    _rewrite_env_values(env_vars, replacements=replacements,
                        service_port_map=service_port_map)
    return env_vars


def _convert_command(container: dict, env_dict: dict[str, str]) -> dict:
    """Convert K8s command/args to compose entrypoint/command with variable resolution."""
    result = {}
    if "command" in container:
        resolved = _resolve_k8s_var_refs(container["command"], env_dict)
        result["entrypoint"] = _escape_shell_vars_for_compose(resolved)
    if "args" in container:
        resolved = _resolve_k8s_var_refs(container["args"], env_dict)
        result["command"] = _escape_shell_vars_for_compose(resolved)
    return result

# --- core.volumes ---




def _build_vol_map(pod_volumes: list,
                    volume_claim_templates: list | None = None) -> dict:
    """Build a map of volume name → volume source from pod spec volumes.

    For StatefulSets, volumeClaimTemplates define implicit PVC volumes
    whose name matches the template metadata.name.
    """
    vol_map = {}
    for vct in (volume_claim_templates or []):
        vname = vct.get("metadata", {}).get("name", "")
        if vname:
            vol_map[vname] = {"type": "pvc", "claim": vname}
    for v in pod_volumes:
        vname = v.get("name", "")
        if "persistentVolumeClaim" in v:
            vol_map[vname] = {"type": "pvc", "claim": v["persistentVolumeClaim"].get("claimName", "")}
        elif "configMap" in v:
            vol_map[vname] = {"type": "configmap", "name": v["configMap"].get("name", ""),
                              "items": v["configMap"].get("items")}
        elif "secret" in v:
            vol_map[vname] = {"type": "secret", "name": v["secret"].get("secretName", ""),
                              "items": v["secret"].get("items")}
        elif "emptyDir" in v:
            vol_map[vname] = {"type": "emptydir"}
        else:
            vol_map[vname] = {"type": "unknown"}
    return vol_map


def _resolve_host_path(host_path: str, volume_root: str) -> str:
    """Resolve host_path: bare names are prefixed with volume_root, explicit paths kept as-is."""
    if host_path.startswith(("/", "./", "../")):
        return host_path
    return f"{volume_root}/{host_path}"


def _convert_pvc_mount(claim: str, mount_path: str, pvc_names: set,
                       config: dict, warnings: list[str]) -> str:
    """Convert a PVC volume mount to a compose volume string."""
    pvc_names.add(claim)
    vol_cfg = config.get("volumes", {}).get(claim)
    if vol_cfg and isinstance(vol_cfg, dict) and "host_path" in vol_cfg:
        resolved = _resolve_host_path(vol_cfg["host_path"], config.get("volume_root", "./data"))
        return f"{resolved}:{mount_path}"
    if vol_cfg is not None:
        return f"{claim}:{mount_path}"
    warnings.append(f"PVC '{claim}' has no mapping in helmfile2compose.yaml — add it manually")
    return f"{claim}:{mount_path}"


def _generate_configmap_files(cm_name: str, cm_data: dict, output_dir: str,
                              generated_cms: set,
                              replacements: list[dict] | None = None,
                              service_port_map: dict | None = None) -> str:
    """Write ConfigMap data entries as files. Returns the directory path (relative)."""
    rel_dir = os.path.join("configmaps", cm_name)
    abs_dir = os.path.join(output_dir, rel_dir)
    if cm_name not in generated_cms:
        generated_cms.add(cm_name)
        os.makedirs(abs_dir, exist_ok=True)
        for key, value in cm_data.items():
            rewritten = str(value)
            if service_port_map:
                rewritten = _apply_port_remap(rewritten, service_port_map)
            if replacements:
                rewritten = apply_replacements(rewritten, replacements)
            file_path = os.path.join(abs_dir, key)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(rewritten)
    return f"./{rel_dir}"


def _resolve_secret_keys(secret: dict, items: list | None) -> list[tuple[str, str]]:
    """Return (key, output_filename) pairs for a Secret volume mount."""
    if items:
        keys = [item["key"] for item in items if "key" in item]
    else:
        keys = list((secret.get("data") or {}).keys()) + list((secret.get("stringData") or {}).keys())
    result = []
    for key in keys:
        out_name = key
        if items:
            for item in items:
                if item.get("key") == key and "path" in item:
                    out_name = item["path"]
                    break
        result.append((key, out_name))
    return result


def _generate_secret_files(sec_name: str, secret: dict, items: list | None,
                           output_dir: str, generated_secrets: set,
                           warnings: list[str],
                           replacements: list[dict] | None = None) -> str:
    """Write Secret data entries as files. Returns the directory path (relative)."""
    rel_dir = os.path.join("secrets", sec_name)
    abs_dir = os.path.join(output_dir, rel_dir)
    if sec_name not in generated_secrets:
        generated_secrets.add(sec_name)
        os.makedirs(abs_dir, exist_ok=True)
        for key, out_name in _resolve_secret_keys(secret, items):
            val = _secret_value(secret, key)
            if val is None:
                warnings.append(f"Secret '{sec_name}' key '{key}' could not be decoded — skipped")
                continue
            if replacements:
                val = apply_replacements(val, replacements)
            with open(os.path.join(abs_dir, out_name), "w", encoding="utf-8") as f:
                f.write(val)
    return f"./{rel_dir}"


def _convert_data_mount(data_dir: str, vm: dict) -> str:
    """Build a bind-mount string for a configmap/secret directory, with optional subPath."""
    mount_path = vm.get("mountPath", "")
    sub_path = vm.get("subPath")
    if sub_path:
        return f"{data_dir}/{sub_path}:{mount_path}:ro"
    return f"{data_dir}:{mount_path}:ro"


def _convert_volume_mounts(volume_mounts: list, pod_volumes: list, pvc_names: set,
                           config: dict, workload_name: str, warnings: list[str],
                           configmaps: dict | None = None, secrets: dict | None = None,
                           output_dir: str = ".", generated_cms: set | None = None,
                           generated_secrets: set | None = None,
                           replacements: list[dict] | None = None,
                           service_port_map: dict | None = None,
                           volume_claim_templates: list | None = None) -> list[str]:
    """Convert volumeMounts to docker-compose volume strings."""
    vol_map = _build_vol_map(pod_volumes, volume_claim_templates)
    result = []
    for vm in volume_mounts:
        source = vol_map.get(vm.get("name", ""), {})
        mount_path = vm.get("mountPath", "")
        vol_type = source.get("type")

        if vol_type == "pvc":
            result.append(_convert_pvc_mount(source["claim"], mount_path, pvc_names, config, warnings))
        elif vol_type == "emptydir":
            result.append(mount_path)
        elif vol_type == "configmap" and configmaps is not None:
            cm = configmaps.get(source["name"])
            if cm is None:
                warnings.append(f"ConfigMap '{source['name']}' referenced by {workload_name} not found")
                continue
            cm_dir = _generate_configmap_files(source["name"], cm.get("data") or {},
                                               output_dir, generated_cms,
                                               replacements=replacements,
                                               service_port_map=service_port_map)
            result.append(_convert_data_mount(cm_dir, vm))
        elif vol_type == "secret" and secrets is not None:
            sec = secrets.get(source["name"])
            if sec is None:
                warnings.append(f"Secret '{source['name']}' referenced by {workload_name} not found")
                continue
            sec_dir = _generate_secret_files(source["name"], sec, source.get("items"),
                                             output_dir, generated_secrets, warnings,
                                             replacements=replacements)
            result.append(_convert_data_mount(sec_dir, vm))

    return result

# --- core.services ---



def _resolve_named_port(name: str, container_ports: list) -> int | str:
    """Resolve a named port (e.g. 'http') to its numeric containerPort."""
    for cp in container_ports:
        if cp.get("name") == name:
            return cp["containerPort"]
    return name  # fallback: return as-is if not found


def _index_workloads(manifests: dict) -> list[tuple[dict, str]]:
    """Index workload labels → workload name for Deployments and StatefulSets."""
    result = []
    for kind in WORKLOAD_KINDS:
        for m in manifests.get(kind, []):
            meta = m.get("metadata", {})
            result.append((meta.get("labels") or {}, meta.get("name", "")))
    return result


def _match_selector(selector: dict, workloads: list[tuple[dict, str]]) -> str | None:
    """Find the workload name that matches a K8s Service selector."""
    for wl_labels, wl_name in workloads:
        if all(wl_labels.get(k) == v for k, v in selector.items()):
            return wl_name
    return None


def _build_alias_map(manifests: dict, services_by_selector: dict) -> dict[str, str]:
    """Build a map of K8s Service names → compose service names.

    Covers two cases:
    - ClusterIP services whose name differs from the workload they select
    - ExternalName services that alias another service
    """
    alias_map: dict[str, str] = {}
    workloads = _index_workloads(manifests)

    # ClusterIP services whose name differs from the workload
    for svc_name, svc_info in services_by_selector.items():
        selector = svc_info.get("selector", {})
        if not selector:
            continue
        wl_name = _match_selector(selector, workloads)
        if wl_name and svc_name != wl_name:
            alias_map[svc_name] = wl_name

    # ExternalName services: resolve target → compose service name
    known_workloads = {wl_name for _, wl_name in workloads}
    for svc_manifest in manifests.get("Service", []):
        spec = svc_manifest.get("spec", {})
        if spec.get("type") != "ExternalName":
            continue
        svc_name = svc_manifest.get("metadata", {}).get("name", "")
        target = _K8S_DNS_RE.sub(r'\1', spec.get("externalName", ""))
        compose_name = alias_map.get(target, target)
        if compose_name in known_workloads:
            alias_map[svc_name] = compose_name

    return alias_map


def _build_network_aliases(services_by_selector: dict,
                           alias_map: dict[str, str]) -> dict[str, list[str]]:
    """Build Docker Compose network aliases for each compose service.

    For each K8s Service, resolve its compose service name (via alias_map or
    direct match) and add FQDN aliases (svc.ns.svc.cluster.local, svc.ns.svc,
    svc.ns) plus a short alias if the K8s Service name differs from the compose
    service name.

    Returns {compose_service_name: [alias1, alias2, ...]}.
    """
    aliases: dict[str, list[str]] = {}
    for svc_name, svc_info in services_by_selector.items():
        ns = svc_info.get("namespace", "")
        compose_name = alias_map.get(svc_name, svc_name)
        svc_aliases = aliases.setdefault(compose_name, [])
        # Short alias: K8s Service name if it differs from the compose service
        if svc_name != compose_name and svc_name not in svc_aliases:
            svc_aliases.append(svc_name)
        # FQDN variants (only if namespace is known)
        if ns:
            for fqdn in (f"{svc_name}.{ns}.svc.cluster.local",
                         f"{svc_name}.{ns}.svc",
                         f"{svc_name}.{ns}"):
                if fqdn not in svc_aliases:
                    svc_aliases.append(fqdn)
    return aliases


def _build_service_port_map(manifests: dict, services_by_selector: dict) -> dict:
    """Build a map of (service_name, service_port) → container_port.

    Ingress backends reference Service ports, but in compose we talk directly
    to containers.  This resolves the chain: service port → targetPort → containerPort.
    """
    # Index workload labels → container ports
    workload_ports: dict[str, list] = {}
    for kind in WORKLOAD_KINDS:
        for m in manifests.get(kind, []):
            name = m.get("metadata", {}).get("name", "")
            containers = ((m.get("spec") or {}).get("template") or {}).get("spec") or {}
            containers = containers.get("containers") or []
            all_ports = []
            for c in containers:
                all_ports.extend(c.get("ports") or [])
            workload_ports[name] = all_ports

    workloads = _index_workloads(manifests)
    port_map: dict = {}
    for svc_name, svc_info in services_by_selector.items():
        selector = svc_info.get("selector", {})
        if not selector:
            continue
        wl_name = _match_selector(selector, workloads)
        matched_ports = workload_ports.get(wl_name, []) if wl_name else []

        for sp in svc_info.get("ports", []):
            svc_port_num = sp.get("port")
            if svc_port_num is None:
                continue
            target = sp.get("targetPort", svc_port_num)
            if isinstance(target, str):
                target = _resolve_named_port(target, matched_ports)
            port_map[(svc_name, svc_port_num)] = target
            if sp.get("name"):
                port_map[(svc_name, sp["name"])] = target

    _expand_fqdn_keys(port_map, services_by_selector)
    return port_map


def _expand_fqdn_keys(port_map: dict, services_by_selector: dict) -> None:
    """Add FQDN variants so _apply_port_remap matches both "svc:80" and
    "svc.ns.svc.cluster.local:80"."""
    fqdn_entries: dict = {}
    for (svc_name, svc_port), container_port in port_map.items():
        ns = services_by_selector.get(svc_name, {}).get("namespace", "")
        if not ns:
            continue
        for fqdn in (f"{svc_name}.{ns}.svc.cluster.local",
                     f"{svc_name}.{ns}.svc",
                     f"{svc_name}.{ns}"):
            fqdn_entries[(fqdn, svc_port)] = container_port
    port_map.update(fqdn_entries)

# --- core.ingress ---



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
    priority = 900

    def convert(self, _kind: str, manifests: list[dict], ctx: ConvertContext) -> ProviderResult:
        """Convert all Ingress manifests via rewriter dispatch."""
        entries = []
        for m in manifests:
            rewriter = self._find_rewriter(m, ctx)
            entries.extend(rewriter.rewrite(m, ctx))
        services = {}
        if entries and not ctx.config.get("disable_ingress"):
            services = self.build_service(entries, ctx)
        return ProviderResult(services=services, ingress_entries=entries)

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

# --- core.extensions ---




def _discover_extension_files(extensions_dir):
    """Find .py files in extensions dir + one level into subdirectories."""
    py_files = []
    for entry in sorted(os.listdir(extensions_dir)):
        full = os.path.join(extensions_dir, entry)
        if entry.startswith(('_', '.')):
            continue
        if entry.endswith('.py') and os.path.isfile(full):
            py_files.append(full)
        elif os.path.isdir(full):
            for sub in sorted(os.listdir(full)):
                sub_full = os.path.join(full, sub)
                if (sub.endswith('.py') and not sub.startswith(('_', '.'))
                        and os.path.isfile(sub_full)):
                    py_files.append(sub_full)
    return py_files


def _is_converter_class(obj, mod_name):
    """Check if obj is a converter class defined in the given module."""
    return (isinstance(obj, type)
            and hasattr(obj, 'kinds') and isinstance(obj.kinds, (list, tuple))
            and hasattr(obj, 'convert') and callable(obj.convert)
            and obj.__module__ == mod_name)


def _is_transform_class(obj, mod_name):
    """Check if obj is a transform class defined in the given module."""
    return (isinstance(obj, type)
            and hasattr(obj, 'transform') and callable(getattr(obj, 'transform'))
            and not hasattr(obj, 'kinds')
            and obj.__module__ == mod_name)


def _load_module(filepath):
    """Load a single extension module, return it or None on failure."""
    parent = str(Path(filepath).parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    mod_name = f"h2c_op_{Path(filepath).stem}"
    spec = importlib.util.spec_from_file_location(mod_name, filepath)
    if spec is None or spec.loader is None:
        return None
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Warning: failed to load {filepath}: {exc}", file=sys.stderr)
        return None


def _classify_module(module, converters, transforms, rewriters):
    """Classify classes in a module into converters, transforms, and rewriters."""
    mod_name = module.__name__
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if _is_converter_class(obj, mod_name):
            converters.append(obj())
        elif _is_rewriter_class(obj, mod_name):
            rewriters.append(obj())
        elif _is_transform_class(obj, mod_name):
            transforms.append(obj())


def _log_loaded(converters, transforms, rewriters):
    """Log loaded extension classes to stderr."""
    if converters:
        loaded = ", ".join(
            f"{type(c).__name__} ({', '.join(c.kinds)})" for c in converters)
        print(f"Loaded extensions: {loaded}", file=sys.stderr)
    if transforms:
        loaded = ", ".join(type(t).__name__ for t in transforms)
        print(f"Loaded transforms: {loaded}", file=sys.stderr)
    if rewriters:
        loaded = ", ".join(
            f"{type(r).__name__} ({r.name})" for r in rewriters)
        print(f"Loaded rewriters: {loaded}", file=sys.stderr)


def _load_extensions(extensions_dir):
    """Load converter, transform, and rewriter classes from an extensions directory."""
    converters = []
    transforms = []
    rewriters = []
    for filepath in _discover_extension_files(extensions_dir):
        module = _load_module(filepath)
        if module:
            _classify_module(module, converters, transforms, rewriters)

    # Sort by priority (lower = earlier). Default 1000.
    converters.sort(key=lambda c: getattr(c, 'priority', 1000))
    transforms.sort(key=lambda t: getattr(t, 'priority', 1000))
    rewriters.sort(key=lambda r: getattr(r, 'priority', 1000))
    _log_loaded(converters, transforms, rewriters)
    return converters, transforms, rewriters


def _override_rewriters(extra_rewriters, rewriters):
    """Override built-in rewriters with external ones sharing the same name."""
    if not extra_rewriters:
        return
    ext_names = {rw.name for rw in extra_rewriters}
    overridden = ext_names & {rw.name for rw in rewriters}
    if overridden:
        rewriters[:] = [rw for rw in rewriters if rw.name not in ext_names]
        for name in sorted(overridden):
            print(f"Rewriter overrides built-in: {name}", file=sys.stderr)
    rewriters[0:0] = extra_rewriters


def _check_duplicate_kinds(extra_converters):
    """Check for duplicate kind claims between extension converters. Exits on conflict."""
    ext_kind_owners: dict[str, str] = {}
    for c in extra_converters:
        for k in c.kinds:
            if k in ext_kind_owners:
                print(f"Error: kind '{k}' claimed by both "
                      f"{ext_kind_owners[k]} and "
                      f"{type(c).__name__} (extensions)",
                      file=sys.stderr)
                sys.exit(1)
            ext_kind_owners[k] = type(c).__name__
    return ext_kind_owners


def _override_converters(ext_kind_owners, converters):
    """Override built-in converters for kinds claimed by extensions."""
    overridden = set(ext_kind_owners)
    for c in converters:
        lost = overridden & set(c.kinds)
        if lost:
            c.kinds = [k for k in c.kinds if k not in overridden]
            print(f"Extension overrides built-in {type(c).__name__} "
                  f"for: {', '.join(sorted(lost))}", file=sys.stderr)


def _register_extensions(extra_converters, extra_transforms, extra_rewriters,
                         converters, transforms, rewriters, converted_kinds):
    """Register loaded extensions into the provided registries."""
    transforms.extend(extra_transforms)
    transforms.sort(key=lambda t: getattr(t, 'priority', 1000))
    _override_rewriters(extra_rewriters, rewriters)
    ext_kind_owners = _check_duplicate_kinds(extra_converters)
    _override_converters(ext_kind_owners, converters)
    converters[0:0] = extra_converters
    converted_kinds.update(k for c in extra_converters for k in c.kinds)

# --- core.convert ---



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
    # Idempotent — safe on services whose env vars were already rewritten by a provider.
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

# --- io.parsing ---




def _helmfile_list_namespaces(helmfile_path: str,
                              environment: str | None = None) -> dict[str, str]:
    """Run ``helmfile list`` and return a release-name → namespace mapping."""
    cmd = ["helmfile", "--file", helmfile_path]
    if environment:
        cmd.extend(["--environment", environment])
    cmd.extend(["list", "--output", "json"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        releases = json.loads(result.stdout)
        return {r["name"]: r.get("namespace", "") for r in releases if r.get("namespace")}
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as exc:
        print(f"⚠ helmfile list failed ({exc.__class__.__name__}), "
              f"namespace inference will rely on manifest metadata only",
              file=sys.stderr)
        return {}


def run_helmfile_template(helmfile_dir: str, output_dir: str,
                          environment: str | None = None) -> tuple[str, dict[str, str]]:
    """Run helmfile template and return (rendered_dir, release_ns_map)."""
    rendered_dir = os.path.join(output_dir, ".helmfile-rendered")
    if os.path.exists(rendered_dir):
        shutil.rmtree(rendered_dir)
    os.makedirs(rendered_dir)
    # helmfile auto-detects .gotmpl extension
    helmfile_path = os.path.join(helmfile_dir, "helmfile.yaml")
    if not os.path.exists(helmfile_path):
        gotmpl = helmfile_path + ".gotmpl"
        if os.path.exists(gotmpl):
            helmfile_path = gotmpl
    cmd = ["helmfile", "--file", helmfile_path]
    if environment:
        cmd.extend(["--environment", environment])
    cmd.extend(["template", "--output-dir", rendered_dir])
    print(f"Running: {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, check=True)
    # Nested helmfiles: helmfile creates per-child .helmfile-rendered dirs
    # instead of putting everything in the --output-dir target. Consolidate.
    helmfile_root = Path(helmfile_dir).resolve()
    main_rendered = Path(rendered_dir).resolve()
    for nested in sorted(helmfile_root.rglob(".helmfile-rendered")):
        if nested.resolve() == main_rendered:
            continue
        for yaml_file in nested.rglob("*.yaml"):
            rel = yaml_file.relative_to(nested)
            dest = main_rendered / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(yaml_file, dest)
        shutil.rmtree(nested)
    release_ns_map = _helmfile_list_namespaces(helmfile_path, environment)
    return rendered_dir, release_ns_map


def parse_manifests(rendered_dir: str) -> dict[str, list[dict]]:
    """Load all YAML files from rendered_dir, classify by kind.

    Each manifest gets an internal ``_h2c_release_dir`` annotation (the
    first path component relative to *rendered_dir*) so that downstream
    steps can group manifests by helmfile release.
    """
    manifests: dict[str, list[dict]] = {}
    rendered = Path(rendered_dir)
    for yaml_file in sorted(rendered.rglob("*.yaml")):
        # First path component relative to rendered_dir = release directory
        rel = yaml_file.relative_to(rendered)
        release_dir = rel.parts[0] if rel.parts else ""
        try:
            with open(yaml_file, encoding="utf-8") as f:
                for doc in yaml.safe_load_all(f):
                    if not doc or not isinstance(doc, dict):
                        continue
                    doc["_h2c_release_dir"] = release_dir
                    kind = doc.get("kind", "Unknown")
                    manifests.setdefault(kind, []).append(doc)
        except yaml.YAMLError as exc:
            print(f"⚠ Skipping {yaml_file.name}: {exc.__class__.__name__}",
                  file=sys.stderr)
    return manifests


def _extract_release_name(release_dir: str) -> str:
    """Extract the release name from a helmfile output directory name.

    Directory format: ``helmfile.yaml-<hash>-<release-name>`` or just ``<name>``.
    """
    # "helmfile.yaml" prefix is constant, followed by 8-char hex hash
    # e.g. "helmfile.yaml-01df6c56-minio" → "minio"
    prefix = "helmfile.yaml-"
    if release_dir.startswith(prefix):
        rest = release_dir[len(prefix):]
        # Skip the hash part (first segment before '-')
        idx = rest.find("-")
        return rest[idx + 1:] if idx >= 0 else rest
    return release_dir


def _collect_known_namespaces(manifests: dict[str, list[dict]]) -> set[str]:
    """Collect all namespaces seen in manifests (declared + referenced)."""
    known = {m.get("metadata", {}).get("name", "")
             for m in manifests.get("Namespace", [])} - {""}
    for kind_list in manifests.values():
        for m in kind_list:
            ns = m.get("metadata", {}).get("namespace", "")
            if ns:
                known.add(ns)
    return known


def _build_dir_ns_map(manifests: dict[str, list[dict]],
                      release_ns_map: dict[str, str] | None = None) -> dict[str, str]:
    """Build a mapping of release directory → namespace.

    Strategy (each phase fills gaps left by the previous):
    1. Sibling inference — any manifest in the same release dir that has a namespace
    2. Namespace/release matching — match release name against known namespaces
    3. ``helmfile list`` data — from *release_ns_map* (only when using ``--helmfile-dir``)
    """
    all_release_dirs: set[str] = set()
    dir_ns: dict[str, str] = {}
    for kind_list in manifests.values():
        for m in kind_list:
            rd = m.get("_h2c_release_dir", "")
            if rd:
                all_release_dirs.add(rd)
                ns = m.get("metadata", {}).get("namespace", "")
                if ns and rd not in dir_ns:
                    dir_ns[rd] = ns

    known_ns = _collect_known_namespaces(manifests)
    for rd in all_release_dirs - dir_ns.keys():
        release_name = _extract_release_name(rd)
        if release_name in known_ns:
            dir_ns[rd] = release_name
        elif release_ns_map and release_name in release_ns_map:
            dir_ns[rd] = release_ns_map[release_name]
    return dir_ns


def _infer_namespaces(manifests: dict[str, list[dict]],
                      release_ns_map: dict[str, str] | None = None) -> None:
    """Fill missing ``metadata.namespace`` from sibling manifests or *release_ns_map*."""
    dir_ns = _build_dir_ns_map(manifests, release_ns_map)
    for kind_list in manifests.values():
        for m in kind_list:
            if not m.get("metadata", {}).get("namespace", ""):
                rd = m.get("_h2c_release_dir", "")
                if rd in dir_ns:
                    m.setdefault("metadata", {})["namespace"] = dir_ns[rd]

# --- io.config ---




def _migrate_config(cfg: dict) -> bool:
    """Migrate legacy config keys to v3.1 names. Returns True if migration happened."""
    migrated = False

    # disableCaddy → disable_ingress
    if "disableCaddy" in cfg:
        cfg["disable_ingress"] = cfg.pop("disableCaddy")
        migrated = True

    # ingressTypes → ingress_types
    if "ingressTypes" in cfg:
        cfg["ingress_types"] = cfg.pop("ingressTypes")
        migrated = True

    # caddy_email → extensions.caddy.email
    if "caddy_email" in cfg:
        cfg.setdefault("extensions", {}).setdefault("caddy", {})["email"] = cfg.pop("caddy_email")
        migrated = True

    # caddy_tls_internal → extensions.caddy.tls_internal
    if "caddy_tls_internal" in cfg:
        cfg.setdefault("extensions", {}).setdefault("caddy", {})["tls_internal"] = cfg.pop("caddy_tls_internal")
        migrated = True

    # helmfile2ComposeVersion → delete (no longer written)
    if "helmfile2ComposeVersion" in cfg:
        del cfg["helmfile2ComposeVersion"]
        migrated = True

    return migrated


def load_config(path: str) -> dict:
    """Load helmfile2compose.yaml or return empty config."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    if _migrate_config(cfg):
        print("Config migrated to v3.1 key names in memory", file=sys.stderr)

    cfg.setdefault("volume_root", "./data")
    cfg.setdefault("volumes", {})
    cfg.setdefault("exclude", [])
    return cfg


def save_config(path: str, config: dict) -> None:
    """Write helmfile2compose.yaml."""
    header = "# Configuration descriptor for https://github.com/helmfile2compose\n\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

# --- io.output ---




def write_compose(services: dict, config: dict, output_dir: str,
                  compose_file: str = "compose.yml") -> None:
    """Write compose file."""
    compose = {}
    if config.get("name"):
        compose["name"] = config["name"]
    compose["services"] = services

    # Add top-level named volumes
    named_volumes = {}
    for vol_name, vol_cfg in config.get("volumes", {}).items():
        if isinstance(vol_cfg, dict) and "host_path" not in vol_cfg:
            named_volumes[vol_name] = vol_cfg
    if named_volumes:
        compose["volumes"] = named_volumes

    # External network override
    ext_network = config.get("network")
    if ext_network:
        compose["networks"] = {"default": {"external": True, "name": ext_network}}

    has_sidecars = any("container_name" in s for s in services.values())

    path = os.path.join(output_dir, compose_file)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Generated by helmfile2compose — do not edit manually\n")
        if has_sidecars:
            f.write("# WARNING: Sidecar containers use container_name for network sharing.\n")
            f.write("# Do not use 'docker compose -p' — rename via helmfile2compose.yaml instead.\n")
        yaml.dump(compose, f, default_flow_style=False, sort_keys=False)
    print(f"Wrote {path}", file=sys.stderr)


def emit_warnings(warnings: list[str]) -> None:
    """Print all warnings to stderr."""
    for w in warnings:
        print(f"⚠ {w}", file=sys.stderr)

# --- cli ---




def _init_first_run(config: dict, manifests: dict, args) -> None:
    """Set project name and auto-exclude K8s-only workloads on first run."""
    source_dir = args.helmfile_dir if not args.from_dir else args.from_dir
    config["name"] = os.path.basename(os.path.realpath(source_dir))
    for kind in WORKLOAD_KINDS:
        for m in manifests.get(kind, []):
            name = m.get("metadata", {}).get("name", "")
            if any(p in name for p in AUTO_EXCLUDE_PATTERNS):
                if name not in config["exclude"]:
                    config["exclude"].append(name)
    if config["exclude"]:
        print(
            f"Auto-excluded K8s-only workloads: {', '.join(config['exclude'])}",
            file=sys.stderr,
        )


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Convert helmfile template output to compose.yml + Caddyfile"
    )
    parser.add_argument(
        "--helmfile-dir", default=".",
        help="Directory containing helmfile.yaml (default: .)",
    )
    parser.add_argument(
        "-e", "--environment",
        help="Helmfile environment to use (e.g. local, production)",
    )
    parser.add_argument(
        "--from-dir",
        help="Skip helmfile template, read pre-rendered YAML from this directory",
    )
    parser.add_argument(
        "--output-dir", default=".",
        help="Where to write compose.yml, Caddyfile, and helmfile2compose.yaml (default: .)",
    )
    parser.add_argument(
        "--compose-file", default="compose.yml",
        help="Name of the generated compose file (default: compose.yml)",
    )
    parser.add_argument(
        "--extensions-dir",
        help="Directory containing h2c extension modules (converters, transforms, rewriters)",
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # Step 1: get rendered manifests
    release_ns_map: dict[str, str] | None = None
    if args.from_dir:
        rendered_dir = args.from_dir
    else:
        rendered_dir, release_ns_map = run_helmfile_template(
            args.helmfile_dir, args.output_dir, args.environment)

    # Step 2: parse
    manifests = parse_manifests(rendered_dir)
    _infer_namespaces(manifests, release_ns_map)
    kinds = {k: len(v) for k, v in manifests.items()}
    print(f"Parsed manifests: {kinds}", file=sys.stderr)

    # Step 3: load config
    config_path = os.path.join(args.output_dir, "helmfile2compose.yaml")
    first_run = not os.path.exists(config_path)
    config = load_config(config_path)

    if first_run:
        _init_first_run(config, manifests, args)

    # Step 3b: load extensions
    if args.extensions_dir:
        if not os.path.isdir(args.extensions_dir):
            print(f"Extensions directory not found: {args.extensions_dir}", file=sys.stderr)
            sys.exit(1)
        extra_converters, extra_transforms, extra_rewriters = _load_extensions(args.extensions_dir)
        _register_extensions(extra_converters, extra_transforms, extra_rewriters,
                             _CONVERTERS, _TRANSFORMS, _REWRITERS, CONVERTED_KINDS)

    # Step 4: convert
    services, ingress_entries, warnings = convert(manifests, config, output_dir=args.output_dir,
                                                  first_run=first_run)

    # Step 5: emit warnings
    emit_warnings(warnings)

    # Step 6: write outputs
    if not services:
        print("No services generated — nothing to write.", file=sys.stderr)
        sys.exit(2)

    write_compose(services, config, args.output_dir, compose_file=args.compose_file)

    # Write ingress config via the active IngressProvider
    ingress_provider = next(
        (c for c in _CONVERTERS if isinstance(c, IngressProvider)), None)
    if ingress_provider and ingress_entries:
        ingress_provider.write_config(ingress_entries, args.output_dir, config)

    if first_run:
        save_config(config_path, config)
        print(f"Wrote {config_path}", file=sys.stderr)
        print(
            "\n⚠ First run — helmfile2compose.yaml was created and likely needs manual edits.\n"
            "  Review exclude list, volume mappings, and re-run.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
