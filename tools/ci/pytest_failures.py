"""Failed tests of a pytest run (its JUnit XML report) as annotations on the GitHub Actions run page, so a failure can
be read without the job log (downloading logs needs a signed-in user; annotations are public). Standard library only,
Python 3.9+.

Usage (a CI step with ``if: failure()`` after ``pytest --junitxml=<report>``):
    python tools/ci/pytest_failures.py <report.xml> [title]
It always exits 0: the test step has already failed the job.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_common import annotate, log  # noqa: E402

MAX_ANNOTATIONS = 10  # GitHub shows at most 10 error annotations per step
MAX_E_LINES = 14  # pytest's explanation lines ("E   ...") kept per failure
MAX_MESSAGE = 1800
_LOCATION = re.compile(r"^\S+\.py:\d+: \S")


def failures(report: Path) -> List[Dict[str, str]]:
    """[{test, kind (failure | error), message, where, explanation}] of a pytest JUnit XML report."""
    out: List[Dict[str, str]] = []
    for case in ET.parse(str(report)).getroot().iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            detail = (node.text or "").splitlines()
            where = next((line.strip() for line in reversed(detail) if _LOCATION.match(line.strip())), "")
            explanation = [line.rstrip() for line in detail if line.startswith("E ")][:MAX_E_LINES]
            test = "::".join(part for part in (case.get("classname", ""), case.get("name", "")) if part)
            out.append({"test": test or "?", "kind": kind, "message": (node.get("message") or "").strip(),
                        "where": where, "explanation": "\n".join(explanation)})
    return out


def describe(item: Dict[str, str]) -> str:
    parts = [item["message"] or ("error al preparar la prueba" if item["kind"] == "error" else "fallo")]
    if item["where"]:
        parts.append(item["where"])
    if item["explanation"]:
        parts.append(item["explanation"])
    text = "\n".join(parts)
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def main(argv: Sequence[str]) -> int:
    if not argv:
        log("Uso: python tools/ci/pytest_failures.py <informe.xml> [título]")
        return 0
    report = Path(argv[0])
    title = argv[1] if len(argv) > 1 else "pytest"
    if not report.is_file():
        annotate("warning", f"No hay informe JUnit en {report}: pytest no llegó a escribirlo.", title)
        return 0
    try:
        found = failures(report)
    except (ET.ParseError, OSError) as exc:
        annotate("warning", f"No se pudo leer el informe JUnit {report}: {exc}", title)
        return 0
    if not found:
        log(f"{title}: el informe JUnit no tiene pruebas fallidas.")
        return 0
    log(f"{title}: {len(found)} {'prueba fallida' if len(found) == 1 else 'pruebas fallidas'}")
    for item in found:
        log(f"  {item['test']}")
    for item in found[:MAX_ANNOTATIONS]:
        annotate("error", describe(item), f"{title} · {item['test']}")
    if len(found) > MAX_ANNOTATIONS:
        rest = len(found) - MAX_ANNOTATIONS
        annotate("error", f"… y {rest} {'prueba fallida más' if rest == 1 else 'pruebas fallidas más'} (ver el registro).",
                 title)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
