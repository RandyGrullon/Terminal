"""Improvement ci: tools/ci/portability.py, the Linux portability checks (CI job `portability`). Each check is shown to
catch the problem it exists for on a small tree of its own (they all work on case-insensitive file systems too), and
the repository itself passes them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI_DIR = ROOT / "tools" / "ci"

sys.path.insert(0, str(CI_DIR))
try:
    import portability  # noqa: E402
finally:
    sys.path.remove(str(CI_DIR))


def test_cr_in_files_read_by_linux_tools(tmp_path):
    (tmp_path / "start.sh").write_bytes(b"#!/usr/bin/env bash\r\necho hola\r\n")
    (tmp_path / "Dockerfile").write_bytes(b"FROM python:3.13-slim\n")
    (tmp_path / ".dockerignore").write_bytes(b".git\r\n")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "x.sh").write_bytes(b"echo\n")
    (tmp_path / "README.md").write_bytes(b"CRLF is fine here\r\n")
    paths = [".dockerignore", "Dockerfile", "README.md", "start.sh", "tools/x.sh"]
    assert portability.lf_only(paths) == [".dockerignore", "Dockerfile", "start.sh", "tools/x.sh"]
    assert portability.crlf_problems(tmp_path, paths) == [
        ".dockerignore: tiene finales de línea CRLF (debe ser LF; ver .gitattributes)",
        "start.sh: tiene finales de línea CRLF (debe ser LF; ver .gitattributes)",
    ]


def test_git_index_line_endings_and_exec_bits():
    eol = ["i/crlf  w/crlf  attr/text=auto         \tDockerfile",
           "i/lf    w/lf    attr/text eol=lf       \tstart.sh",
           "i/crlf  w/crlf  attr/text=auto         \tREADME.md"]
    assert portability.index_eol_problems(eol) == ["Dockerfile: en git tiene CRLF (i/crlf); vuelve a añadirlo con LF"]
    stage = ["100644 1111111 0\tstart.sh", "100755 2222222 0\ttools/x.sh"]
    assert portability.exec_bit_problems(stage) == [
        "start.sh: modo 100644 en git (debe ser 100755: git update-index --chmod=+x start.sh)"
    ]


def test_import_case_mismatches_are_found_even_on_case_insensitive_disks(tmp_path):
    src = tmp_path / "frontend" / "src"
    (src / "components").mkdir(parents=True)
    (src / "components" / "TradeCheck.tsx").write_text("export default 1\n", encoding="utf-8")
    (src / "lib.ts").write_text("export const x = 1\n", encoding="utf-8")
    (src / "main.tsx").write_text(
        "import T from './components/tradeCheck'\nimport { x } from './lib'\n"
        "const lazy = () => import('./components/TradeCheck')\nimport './missing.css'\n", encoding="utf-8")
    tests = tmp_path / "frontend" / "tests"
    tests.mkdir()
    # esbuild entries of tests/*.test.mjs resolve from frontend/src (resolveDir: SRC)
    (tests / "a.test.mjs").write_text("const ENTRY = `\nexport { default } from './Components/TradeCheck'\n`\n",
                                      encoding="utf-8")
    paths = ["frontend/src/components/TradeCheck.tsx", "frontend/src/lib.ts", "frontend/src/main.tsx",
             "frontend/tests/a.test.mjs"]
    assert portability.import_case_problems(tmp_path, paths) == [
        "frontend/src/main.tsx:1: «./components/tradeCheck» no coincide en mayúsculas con «components/TradeCheck.tsx» "
        "(falla en Linux)",
        "frontend/tests/a.test.mjs:2: «./Components/TradeCheck» no coincide en mayúsculas con "
        "«components/TradeCheck.tsx» (falla en Linux)",
    ]


def test_dockerfile_copy_sources_must_exist_with_their_exact_case(tmp_path):
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.13-slim AS build\n"
        "COPY --chown=1:1 docs docs\n"
        "COPY Backend/app \\\n    backend/app\n"
        "COPY --from=build /out /out\n"
        "COPY missing.txt /x\n"
        "ADD https://example.invalid/a.tgz /a\n"
        "COPY *.md /docs/\n"
        "# COPY commented out\n", encoding="utf-8")
    assert portability.dockerfile_problems(tmp_path) == [
        "Dockerfile: COPY Backend/app no coincide en mayúsculas con backend/app (falla en Linux)",
        "Dockerfile: COPY missing.txt no existe",
    ]
    real = portability.dockerfile_sources((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert {"backend/requirements.lock", "backend/app", "docs"} <= set(real)


def test_npm_lock_must_list_the_optional_native_packages(tmp_path):
    lock = {"lockfileVersion": 3, "packages": {
        "": {"name": "ui"},
        "node_modules/rollup": {"version": "4.0.0", "optionalDependencies": {
            "@rollup/rollup-linux-x64-gnu": "4.0.0", "fsevents": "~2.3.2"}},
        "node_modules/fsevents": {"version": "2.3.3", "optional": True},
    }}
    (tmp_path / "frontend").mkdir()
    path = tmp_path / "frontend" / "package-lock.json"
    path.write_text(json.dumps(lock), encoding="utf-8")
    assert portability.lockfile_problems(tmp_path) == [
        "frontend/package-lock.json: falta @rollup/rollup-linux-x64-gnu (opcional de rollup); "
        "regenera el lock con npm install"
    ]
    lock["packages"]["node_modules/@rollup/rollup-linux-x64-gnu"] = {"version": "4.0.0", "optional": True}
    path.write_text(json.dumps(lock), encoding="utf-8")
    assert portability.lockfile_problems(tmp_path) == []


def test_walk_fallback_skips_local_data_and_environments(tmp_path):
    for rel in ("data/nexus.db", "backend/app/data/hub.py", "backend/.venv/x.sh", "frontend/node_modules/a.sh",
                "start.sh"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    assert portability.repo_files(tmp_path) == ["backend/app/data/hub.py", "start.sh"]  # no .git here: a walk


def test_the_repository_is_portable():
    assert portability.check(ROOT) == []
