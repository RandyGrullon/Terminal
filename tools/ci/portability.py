"""Linux portability checks of the repository (CI job `portability`). Also worth running on Windows before a push:
Windows and macOS forgive things that break a Linux checkout, the Docker build or the CI runners.

- LF only: shell scripts, the Dockerfile and .dockerignore contain no CR (a CR breaks `#!/usr/bin/env bash` and RUN
  lines); in git as well (index content) when git is available;
- shell scripts tracked by git are executable (mode 100755: `bash start.sh` works without it, `./start.sh` does not);
- relative imports of frontend/src and frontend/tests match the file names exactly, upper / lower case included;
- the Dockerfile's COPY / ADD sources exist with that exact case;
- frontend/package-lock.json lists every optional native package of its dependencies (e.g.
  @rollup/rollup-linux-x64-gnu, @esbuild/linux-x64): without them `npm ci` on Linux installs no binary.

    python tools/ci/portability.py [--root <repo>]      (exit 1 with the list of problems)
"""
from __future__ import annotations

import argparse
import fnmatch
import functools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

LF_ONLY = ("*.sh", "Dockerfile", ".dockerignore")
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "dist", ".tmp", "__pycache__", ".pytest_cache"})
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
RESOLVE_SUFFIXES = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css",
                    "/index.ts", "/index.tsx", "/index.js")
IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|^\s*import\s+|\brequire\s*\(\s*)["'](\.{1,2}/[^"'\n]+)["']""", re.MULTILINE
)


def repo_files(root: Path) -> list[str]:
    """Repository-relative POSIX paths: tracked + untracked-but-not-ignored files when git works, else a walk."""
    if shutil.which("git") and (root / ".git").exists():
        cp = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
                            capture_output=True, stdin=subprocess.DEVNULL)
        if cp.returncode == 0:
            return sorted({p for p in cp.stdout.decode("utf-8", "replace").split("\0") if p and (root / p).is_file()})
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        top = rel == Path(".")
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (top and d == "data")]  # /data = local DB
        out += [(rel / name).as_posix() for name in filenames]
    return sorted(out)


def lf_only(paths: Iterable[str]) -> list[str]:
    return [p for p in paths if any(fnmatch.fnmatchcase(p.rsplit("/", 1)[-1], pat) for pat in LF_ONLY)]


def crlf_problems(root: Path, paths: Iterable[str]) -> list[str]:
    problems = []
    for rel in lf_only(paths):
        if b"\r" in (root / rel).read_bytes():
            problems.append(f"{rel}: tiene finales de línea CRLF (debe ser LF; ver .gitattributes)")
    return problems


def git_lines(root: Path, *args: str) -> list[str] | None:
    if not (shutil.which("git") and (root / ".git").exists()):
        return None
    cp = subprocess.run(["git", *args], cwd=root, capture_output=True, stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        return None
    return cp.stdout.decode("utf-8", "replace").splitlines()


def index_eol_problems(eol_lines: Sequence[str]) -> list[str]:
    """`git ls-files --eol` lines: LF-only files whose committed content has CR."""
    problems = []
    for line in eol_lines:
        info, _, path = line.partition("\t")
        fields = info.split()
        if path and lf_only([path]) and fields and fields[0] in ("i/crlf", "i/mixed"):
            problems.append(f"{path}: en git tiene CRLF ({fields[0]}); vuelve a añadirlo con LF")
    return problems


def exec_bit_problems(stage_lines: Sequence[str]) -> list[str]:
    """`git ls-files -s` lines: tracked *.sh files that are not executable."""
    problems = []
    for line in stage_lines:
        info, _, path = line.partition("\t")
        mode = info.split()[0] if info.split() else ""
        if path.endswith(".sh") and mode != "100755":
            problems.append(f"{path}: modo {mode} en git (debe ser 100755: git update-index --chmod=+x {path})")
    return problems


@functools.lru_cache(maxsize=None)
def _names(directory: str) -> tuple[str, ...]:
    return tuple(os.listdir(directory))


def resolve_exact(base: Path, rel: str) -> tuple[Path | None, str | None]:
    """Resolves ``rel`` from ``base`` comparing names case-insensitively: (path, None) when every component matches
    exactly, (path, real spelling) when only the case differs, (None, None) when nothing matches."""
    current, spelled, exact = base, [], True
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            spelled.append("..")
            continue
        try:
            names = _names(str(current))
        except OSError:
            return None, None
        if part in names:
            match = part
        else:
            folded = [n for n in names if n.lower() == part.lower()]
            if not folded:
                return None, None
            match, exact = folded[0], False
        current = current / match
        spelled.append(match)
    return current, (None if exact else "/".join(spelled))


def resolve_import(base: Path, spec: str) -> tuple[Path | None, str | None]:
    for suffix in RESOLVE_SUFFIXES:
        found, real = resolve_exact(base, spec + suffix)
        if found is not None and found.is_file():
            return found, real
    return None, None


def import_case_problems(root: Path, paths: Iterable[str]) -> list[str]:
    """Relative imports whose case differs from the file on disk. Test files are also resolved from frontend/src
    (the esbuild entries of tests/*.test.mjs use resolveDir = src). Imports that resolve nowhere are left to tsc."""
    src = root / "frontend" / "src"
    problems = []
    for rel in paths:
        if not rel.startswith(("frontend/src/", "frontend/tests/")) or not rel.endswith(SOURCE_SUFFIXES):
            continue
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        for match in IMPORT.finditer(text):
            spec = match.group(1)
            found, real = resolve_import((root / rel).parent, spec)
            if found is None and rel.startswith("frontend/tests/"):
                found, real = resolve_import(src, spec)
            if found is not None and real is not None:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{rel}:{line}: «{spec}» no coincide en mayúsculas con «{real}» (falla en Linux)")
    return problems


def dockerfile_sources(text: str) -> list[str]:
    """Local sources of the COPY / ADD instructions (flags, --from stages, URLs and wildcards skipped)."""
    sources = []
    joined = re.sub(r"\\\r?\n", " ", text)
    for line in joined.splitlines():
        words = line.strip().split()
        if not words or words[0].upper() not in ("COPY", "ADD") or line.strip().startswith("#"):
            continue
        args = [w for w in words[1:] if not w.startswith("--")]
        if any(w.startswith("--from") for w in words[1:]) or len(args) < 2:
            continue
        sources += [s for s in args[:-1] if "://" not in s and not any(ch in s for ch in "*?[")]
    return sources


def dockerfile_problems(root: Path) -> list[str]:
    path = root / "Dockerfile"
    if not path.is_file():
        return []
    problems = []
    for source in dockerfile_sources(path.read_text(encoding="utf-8", errors="replace")):
        found, real = resolve_exact(root, source.strip("/"))
        if found is None:
            problems.append(f"Dockerfile: COPY {source} no existe")
        elif real is not None:
            problems.append(f"Dockerfile: COPY {source} no coincide en mayúsculas con {real} (falla en Linux)")
    return problems


EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def lock_lookup(packages: dict, where: str, name: str) -> str | None:
    """Entry of the lock that Node loads for ``name`` required from ``where``: ``<where>/node_modules/<name>``, then the
    node_modules folder of every enclosing package, up to the project's (None when the lock has none). A second copy
    of a package (e.g. another esbuild under a tool) keeps its binaries next to it, not at the top level."""
    base = where
    while True:
        candidate = f"{base}/node_modules/{name}" if base else f"node_modules/{name}"
        if candidate in packages:
            return candidate
        if not base:
            return None
        cut = base.rfind("/node_modules/")
        base = base[:cut] if cut >= 0 else ""


def lockfile_problems(root: Path) -> list[str]:
    """Optional dependencies (platform binaries) missing from frontend/package-lock.json (npm/cli#4828), looked up the
    way Node resolves them; one pinned to an exact version (esbuild and rollup pin their binaries) must be that one."""
    path = root / "frontend" / "package-lock.json"
    if not path.is_file():
        return []
    try:
        packages = json.loads(path.read_text(encoding="utf-8")).get("packages") or {}
    except ValueError as exc:
        return [f"frontend/package-lock.json no es JSON válido: {exc}"]
    problems = []
    for where, meta in sorted(packages.items()):
        optional = (meta or {}).get("optionalDependencies") or {}
        for name in sorted(optional):
            owner = where.rsplit("node_modules/", 1)[-1] or "frontend"
            found = lock_lookup(packages, where, name)
            if found is None:
                problems.append(f"frontend/package-lock.json: falta {name} (opcional de {owner}); "
                                "regenera el lock con npm install")
                continue
            spec, version = str(optional[name]), (packages[found] or {}).get("version")
            if EXACT_VERSION.fullmatch(spec) and version and version != spec:
                problems.append(f"frontend/package-lock.json: {found} es la versión {version}, pero {owner} pide "
                                f"{name} {spec}; regenera el lock con npm install")
    return problems


def check(root: Path) -> list[str]:
    _names.cache_clear()
    paths = repo_files(root)
    problems = crlf_problems(root, paths)
    eol = git_lines(root, "ls-files", "--eol")
    if eol is not None:
        problems += index_eol_problems(eol)
    stage = git_lines(root, "ls-files", "-s", "--", "*.sh")
    if stage is not None:
        problems += exec_bit_problems(stage)
    problems += import_case_problems(root, paths)
    problems += dockerfile_problems(root)
    problems += lockfile_problems(root)
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Comprobaciones de portabilidad a Linux del repositorio.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    problems = check(args.root.resolve())
    for problem in problems:
        print(f"::error::{problem}" if os.environ.get("GITHUB_ACTIONS") == "true" else f"FALLO {problem}", flush=True)
    if problems:
        print(f"\nPortabilidad: {len(problems)} {'problema' if len(problems) == 1 else 'problemas'}.", flush=True)
        return 1
    print("Portabilidad: sin problemas (LF, permisos, mayúsculas, COPY del Dockerfile, lock de npm).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
