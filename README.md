# dekube-engine

![vibe coded](https://img.shields.io/badge/vibe-coded-ff69b4)
![python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)
![heresy: 9/10](https://img.shields.io/badge/heresy-9%2F10-8b0000)
![public domain](https://img.shields.io/badge/license-public%20domain-brightgreen)

*The bare conversion engine. A temple with no priests.*

This is the core of [dekube](https://dekube.io) — the spec (`pacts/`), the control plane (`core/`), the I/O layer (`io/`), and the CLI. It runs standalone, accepts `--extensions-dir`, and does nothing on its own. No built-in converters, no built-in rewriters. All manifest kinds are unknown. It is pure potential, waiting to be told what to do.

**You probably want [helmfile2compose](https://github.com/dekubeio/helmfile2compose)** — the full distribution with 8 bundled extensions. This repo is for custom distributions and extension development.

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
src/dekube/
├── pacts/          Public contracts (ConvertContext, ConverterResult, IngressRewriter...)
├── core/           Conversion engine (convert, env, volumes, services, ingress, extensions)
├── io/             Input/output (parsing, config, output)
└── cli.py          CLI entry point
```

## Build

```bash
python build.py
# → dekube.py (single-file distribution)
```

Requires `pyyaml`.

## Usage

```bash
# Standalone (does nothing without extensions)
python3 dekube.py --from-dir /tmp/rendered --output-dir .

# With extensions
python3 dekube.py --from-dir /tmp/rendered --extensions-dir ./my-extensions --output-dir .
```

## Related repos

| Repo | Description |
|------|-------------|
| [helmfile2compose](https://github.com/dekubeio/helmfile2compose) | Full distribution (core + 8 bundled extensions) |
| [dekube-manager](https://github.com/dekubeio/dekube-manager) | Package manager + extension registry |
| [dekube-docs](https://docs.dekube.io) | Documentation site |

## License

Public domain.
