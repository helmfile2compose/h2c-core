#!/usr/bin/env python3
"""Build a distribution single-file script from a base + extensions.

Three modes for the base:
  # Local dev — read core sources directly from h2c-core package
  python build-distribution.py helmfile2compose --extensions-dir ./extensions --core-dir ../h2c-core

  # Local — use a pre-built .py as base
  python build-distribution.py kubernetes2simple --extensions-dir ./extensions --base ../helmfile2compose/helmfile2compose.py

  # CI — fetch a distribution from GitHub releases (default: core latest)
  python build-distribution.py helmfile2compose --extensions-dir ./extensions
  python build-distribution.py kubernetes2simple --extensions-dir ./extensions --base-distribution helmfile2compose
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

# Imports to strip (internal cross-references from h2c package)
INTERNAL_IMPORT_RE = re.compile(
    r'^\s*(?:from h2c[\w.]* import .+|import h2c[\w.]*)\s*$'
)

# h2c-core's module list (for --core-dir local mode)
CORE_MODULES = [
    "core/constants.py",
    "pacts/types.py",
    "pacts/helpers.py",
    "pacts/ingress.py",
    "core/env.py",
    "core/volumes.py",
    "core/services.py",
    "core/ingress.py",
    "core/extensions.py",
    "core/convert.py",
    "io/parsing.py",
    "io/config.py",
    "io/output.py",
    "cli.py",
]

SHEBANG = "#!/usr/bin/env python3\n"
PYLINT_DISABLE = "# pylint: disable=too-many-locals\n"

DISTRIBUTIONS_URL = (
    "https://raw.githubusercontent.com/"
    "helmfile2compose/h2c-manager/main/distributions.json"
)


def collect_imports_and_body(path: Path) -> tuple[list[str], list[str]]:
    """Split a module into stdlib/external imports and body lines."""
    imports = []
    body = []
    in_docstring = False
    docstring_delim = None
    in_internal_import = False

    for line in path.read_text().splitlines(keepends=True):
        stripped = line.strip()

        if not in_docstring and not body and not imports:
            if stripped.startswith(('"""', "'''")):
                delim = stripped[:3]
                if stripped.count(delim) >= 2:
                    continue
                in_docstring = True
                docstring_delim = delim
                continue
        if in_docstring:
            if docstring_delim in stripped:
                in_docstring = False
            continue

        if in_internal_import:
            if ")" in stripped:
                in_internal_import = False
            continue

        if INTERNAL_IMPORT_RE.match(line) or stripped.startswith("from __future__"):
            if "(" in stripped and ")" not in stripped:
                in_internal_import = True
            continue

        if (not line[0].isspace()
                and stripped.startswith(("import ", "from "))
                and not stripped.startswith(("from .", "from __future__"))):
            imports.append(line)
            continue

        body.append(line)

    return imports, body


def strip_tail(text: str) -> str:
    """Remove tail blocks (_auto_register, sys.modules hack, __main__ guard).

    Cuts everything from the first _auto_register() call or if __name__ guard
    onwards. Works on both bare core (only __main__) and full distributions
    (_auto_register + sys.modules + __main__).
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "_auto_register()" or stripped.startswith("if __name__"):
            # Walk back to include the preceding comment block
            while i > 0 and lines[i - 1].strip().startswith("#"):
                i -= 1
            # Walk back past blank lines
            while i > 0 and not lines[i - 1].strip():
                i -= 1
            return "".join(lines[:i])
    return text


def fetch_distributions_registry() -> dict:
    """Fetch and parse distributions.json from h2c-manager."""
    try:
        with urllib.request.urlopen(DISTRIBUTIONS_URL) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("distributions", {})
    except Exception as exc:
        print(f"Error fetching distributions registry: {exc}", file=sys.stderr)
        sys.exit(1)


def fetch_base_release(repo: str, filename: str, version: str = "latest") -> str:
    """Download a distribution .py from GitHub releases."""
    if version == "latest":
        url = f"https://github.com/{repo}/releases/latest/download/{filename}"
    else:
        url = f"https://github.com/{repo}/releases/download/{version}/{filename}"
    print(f"Fetching {filename} from {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.read().decode("utf-8")
    except Exception as exc:
        print(f"Error fetching {filename}: {exc}", file=sys.stderr)
        sys.exit(1)


def parse_flat_script(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse a flat .py script into deduplicated imports + body lines."""
    all_imports: dict[str, str] = {}
    body: list[str] = []
    past_header = False
    in_imports = True

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not past_header:
            if (stripped.startswith("#!") or stripped.startswith('"""')
                    or stripped.startswith("# pylint") or not stripped):
                continue
            past_header = True

        if in_imports:
            if stripped.startswith(("import ", "from ")):
                key = stripped
                if key not in all_imports:
                    all_imports[key] = line
                continue
            if not stripped:
                continue
            in_imports = False

        body.append(line)

    return all_imports, body


def discover_extensions(extensions_dir: Path) -> list[Path]:
    """Find .py extension files in extensions dir (skip __init__.py and hidden files)."""
    py_files = []
    for entry in sorted(os.listdir(extensions_dir)):
        full = extensions_dir / entry
        if entry.startswith(('_', '.')):
            continue
        if entry.endswith('.py') and full.is_file():
            py_files.append(full)
    return py_files


def build_core_body_from_local(core_dir: Path) -> tuple[dict[str, str], list[str]]:
    """Read core sources from a local h2c-core checkout."""
    src_dir = core_dir / "src" / "h2c"
    if not src_dir.exists():
        print(f"Error: {src_dir} not found", file=sys.stderr)
        sys.exit(1)

    all_imports: dict[str, str] = {}
    all_bodies: list[str] = []

    for mod_path in CORE_MODULES:
        full_path = src_dir / mod_path
        if not full_path.exists():
            print(f"Error: {full_path} not found", file=sys.stderr)
            sys.exit(1)
        imports, body = collect_imports_and_body(full_path)
        for imp in imports:
            key = imp.strip()
            if key and key not in all_imports:
                all_imports[key] = imp
        section = mod_path.replace(".py", "").replace("/", ".")
        all_bodies.append(f"\n# --- {section} ---\n")
        all_bodies.extend(body)

    return all_imports, all_bodies


def build_base_body_from_file(base_path: Path) -> tuple[dict[str, str], list[str]]:
    """Read a pre-built .py distribution and parse it."""
    if not base_path.exists():
        print(f"Error: {base_path} not found", file=sys.stderr)
        sys.exit(1)
    text = strip_tail(base_path.read_text())
    all_imports, body = parse_flat_script(text)
    label = base_path.stem
    all_bodies: list[str] = [f"\n# --- {label} ---\n"]
    all_bodies.extend(body)
    return all_imports, all_bodies


def build_base_body_from_release(distribution: str, version: str) -> tuple[dict[str, str], list[str]]:
    """Fetch a distribution from GitHub releases and parse it."""
    registry = fetch_distributions_registry()
    entry = registry.get(distribution)
    if not entry:
        print(f"Error: unknown distribution '{distribution}'", file=sys.stderr)
        print(f"  Available: {', '.join(sorted(registry))}", file=sys.stderr)
        sys.exit(1)
    text = strip_tail(fetch_base_release(entry["repo"], entry["file"], version))
    all_imports, body = parse_flat_script(text)
    all_bodies: list[str] = [f"\n# --- {distribution} ---\n"]
    all_bodies.extend(body)
    return all_imports, all_bodies


def main():
    parser = argparse.ArgumentParser(
        description="Build a distribution single-file script from a base + extensions")
    parser.add_argument("name", help="Distribution name (output: <name>.py)")
    parser.add_argument("--extensions-dir", type=Path, required=True,
                        help="Directory containing extension .py files")
    parser.add_argument("--core-dir", type=Path,
                        help="Path to local h2c-core repo (reads package sources directly)")
    parser.add_argument("--base", type=Path,
                        help="Path to a pre-built .py to use as base")
    parser.add_argument("--base-distribution", default="core",
                        help="Distribution name from registry (default: core)")
    parser.add_argument("--base-version", default="latest",
                        help="Distribution version to fetch (default: latest)")
    args = parser.parse_args()

    output = Path(args.name + ".py")
    docstring = f'"""{args.name} — convert helmfile template output to compose.yml + Caddyfile."""\n'

    # Step 1: Build base body
    if args.core_dir:
        print(f"Local dev mode: reading core from {args.core_dir}", file=sys.stderr)
        all_imports, all_bodies = build_core_body_from_local(args.core_dir)
    elif args.base:
        print(f"Local base mode: reading from {args.base}", file=sys.stderr)
        all_imports, all_bodies = build_base_body_from_file(args.base)
    else:
        print(f"CI mode: fetching {args.base_distribution} {args.base_version}",
              file=sys.stderr)
        all_imports, all_bodies = build_base_body_from_release(
            args.base_distribution, args.base_version)

    # Step 2: Concat extension .py files
    if not args.extensions_dir.is_dir():
        print(f"Error: extensions directory not found: {args.extensions_dir}", file=sys.stderr)
        sys.exit(1)

    ext_files = discover_extensions(args.extensions_dir)
    for ext_path in ext_files:
        imports, body = collect_imports_and_body(ext_path)
        for imp in imports:
            key = imp.strip()
            if key and key not in all_imports:
                all_imports[key] = imp
        section = f"extensions.{ext_path.stem}"
        all_bodies.append(f"\n# --- {section} ---\n")
        all_bodies.extend(body)

    # Step 3: Sort imports (stdlib before third-party)
    stdlib_imports = []
    thirdparty_imports = []
    for imp in all_imports.values():
        module = imp.strip().split()[1].split(".")[0]
        if module == "yaml":
            thirdparty_imports.append(imp)
        else:
            stdlib_imports.append(imp)

    # Step 4: Assemble
    lines = [SHEBANG, docstring, PYLINT_DISABLE, "\n"]
    lines.extend(stdlib_imports)
    if thirdparty_imports:
        lines.append("\n")
        lines.extend(thirdparty_imports)
    lines.append("\n")
    lines.extend(all_bodies)

    # Step 5: Append _auto_register() call
    lines.append("\n\n# Auto-register all converter/rewriter/transform classes\n")
    lines.append("_auto_register()\n")

    # Step 6: Register as 'h2c' module so extensions can `from h2c import ...`
    # Must be the same module object (not a copy) so mutable state (_REWRITERS etc.) is shared
    lines.append('\n\nsys.modules.setdefault("h2c", sys.modules[__name__])\n')

    # Step 7: __main__ guard
    lines.append('\n\nif __name__ == "__main__":\n')
    lines.append("    main()\n")

    output.write_text("".join(lines))
    total_lines = "".join(lines).count("\n")
    print(f"Built {output} ({total_lines} lines)")

    # Step 8: Smoke test
    result = subprocess.run(
        [sys.executable, str(output), "--help"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Smoke test FAILED:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("Smoke test passed (--help)")


if __name__ == "__main__":
    main()
