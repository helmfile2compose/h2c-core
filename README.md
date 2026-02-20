# h2c-core

![python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)
![public domain](https://img.shields.io/badge/license-public%20domain-brightgreen)

*The bare conversion engine. A temple with no priests.*

This is the core of [helmfile2compose](https://github.com/helmfile2compose) — the spec (`pacts/`), the control plane (`core/`), the I/O layer (`io/`), and the CLI. It runs standalone, accepts `--extensions-dir`, and does nothing on its own. No built-in converters, no built-in rewriters. All manifest kinds are unknown. It is pure potential, waiting to be told what to do.

**You probably want [helmfile2compose](https://github.com/helmfile2compose/helmfile2compose)** — the full distribution with built-in extensions. This repo is for custom distributions and extension development.

## What it does

Provides the conversion pipeline, extension loader, and CLI — but with empty registries:

```python
_CONVERTERS = []       # no built-in converters
_REWRITERS = []        # no built-in rewriters
CONVERTED_KINDS = set()  # no known kinds
```

Feed it manifests and it will parse them, warn that every kind is unknown, and produce nothing. Load extensions via `--extensions-dir` and it becomes useful.

## Architecture

```
src/helmfile2compose/
├── pacts/          Public contracts (ConvertContext, ConvertResult, IngressRewriter...)
├── core/           Conversion engine (convert, env, volumes, services, ingress, extensions)
├── io/             Input/output (parsing, config, output)
└── cli.py          CLI entry point
```

## Build

```bash
python build.py
# → h2c.py (single-file distribution, ~1265 lines)
```

Requires `pyyaml`.

## Usage

```bash
# Standalone (does nothing without extensions)
python3 h2c.py --from-dir /tmp/rendered --output-dir .

# With extensions
python3 h2c.py --from-dir /tmp/rendered --extensions-dir ./my-extensions --output-dir .
```

## Related repos

| Repo | Description |
|------|-------------|
| [helmfile2compose](https://github.com/helmfile2compose/helmfile2compose) | Full distribution (core + built-in extensions) |
| [h2c-manager](https://github.com/helmfile2compose/h2c-manager) | Package manager + extension registry |
| [helmfile2compose.github.io](https://github.com/helmfile2compose/helmfile2compose.github.io) | Documentation site |

## License

Public domain.
