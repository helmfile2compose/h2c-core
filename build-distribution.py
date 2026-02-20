#!/usr/bin/env python3
"""Build a distribution single-file script from h2c core + extensions.

Two modes:
  # Local dev (reads core sources directly)
  python build-distribution.py helmfile2compose --extensions-dir ./extensions --core-dir ../h2c-core

  # CI (fetches h2c.py from latest release)
  python build-distribution.py helmfile2compose --extensions-dir ./extensions
"""

import argparse
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

        if INTERNAL_IMPORT_RE.match(line):
            if "(" in stripped and ")" not in stripped:
                in_internal_import = True
            continue

        if stripped.startswith(("import ", "from ")) and not stripped.startswith("from ."):
            imports.append(line)
            continue

        body.append(line)

    return imports, body


def fetch_core_release(version: str = "latest") -> str:
    """Download h2c.py from the h2c-core GitHub releases."""
    if version == "latest":
        url = "https://github.com/helmfile2compose/h2c-core/releases/latest/download/h2c.py"
    else:
        url = f"https://github.com/helmfile2compose/h2c-core/releases/download/{version}/h2c.py"
    print(f"Fetching h2c.py from {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.read().decode("utf-8")
    except Exception as exc:
        print(f"Error fetching h2c.py: {exc}", file=sys.stderr)
        sys.exit(1)


def strip_main_guard(text: str) -> str:
    """Remove the if __name__ == '__main__' block from the end of h2c.py."""
    lines = text.splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith('if __name__'):
            return "".join(lines[:i])
    return text


def parse_flat_script(text: str) -> tuple[dict[str, str], list[str]]:
    """Parse a flat h2c.py into deduplicated imports + body lines."""
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


def build_core_body_from_release(version: str) -> tuple[dict[str, str], list[str]]:
    """Fetch h2c.py from a release and parse it."""
    core_text = strip_main_guard(fetch_core_release(version))
    all_imports, core_body = parse_flat_script(core_text)
    all_bodies: list[str] = ["\n# --- core ---\n"]
    all_bodies.extend(core_body)
    return all_imports, all_bodies


def main():
    parser = argparse.ArgumentParser(
        description="Build a distribution single-file script from h2c core + extensions")
    parser.add_argument("name", help="Distribution name (output: <name>.py)")
    parser.add_argument("--extensions-dir", type=Path, required=True,
                        help="Directory containing extension .py files")
    parser.add_argument("--core-dir", type=Path,
                        help="Path to local h2c-core repo (local dev mode)")
    parser.add_argument("--core-version", default="latest",
                        help="h2c-core release version to fetch (CI mode, default: latest)")
    args = parser.parse_args()

    output = Path(args.name + ".py")
    docstring = f'"""{args.name} — convert helmfile template output to compose.yml + Caddyfile."""\n'

    # Step 1: Build core body
    if args.core_dir:
        print(f"Local dev mode: reading core from {args.core_dir}", file=sys.stderr)
        all_imports, all_bodies = build_core_body_from_local(args.core_dir)
    else:
        print(f"CI mode: fetching h2c-core {args.core_version}", file=sys.stderr)
        all_imports, all_bodies = build_core_body_from_release(args.core_version)

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

    # Step 6: Register as 'h2c' module (needed because output file isn't named h2c.py)
    lines.append('\n\n# Allow extensions to "from h2c import ..." at runtime\n')
    lines.append('import types as _types\n')
    lines.append('sys.modules.setdefault("h2c", _types.ModuleType("h2c"))\n')
    lines.append('sys.modules["h2c"].__dict__.update(\n')
    lines.append('    {k: v for k, v in globals().items() if not k.startswith("_")}\n')
    lines.append(')\n')

    # Step 7: __main__ guard
    lines.append('\n\nif __name__ == "__main__":\n')
    lines.append("    main()\n")

    output.write_text("".join(lines))
    print(f"Built {output} ({sum(1 for l in lines if l.strip())} non-empty lines)")

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
