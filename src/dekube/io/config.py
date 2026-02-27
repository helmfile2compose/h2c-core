"""Configuration file handling — load/save dekube.yaml."""

import os
import sys

import yaml


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
    """Load dekube.yaml (or legacy helmfile2compose.yaml) or return empty config."""
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
    """Write dekube.yaml."""
    header = "# Configuration descriptor for https://dekube.io\n\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
