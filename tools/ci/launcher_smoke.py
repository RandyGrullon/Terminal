"""Linux launcher check (CI job `launcher`): ``bash start.sh --no-browser`` on a clean checkout creates backend/.venv,
installs backend/requirements-dev.txt, runs npm ci, builds frontend/dist and starts the server, like a new user would.
Then: /api/health in local mode (SQLite, no login, every module loaded), the built UI (index.html and one of its
/assets/ files) and a clean stop with SIGTERM. On Linux the venv also gets uvloop (uvicorn[standard]); the server
always runs on the asyncio loop anyway (app.main passes loop="asyncio" to uvicorn), so this checks the launcher itself.

POSIX only (process groups):  python3 tools/ci/launcher_smoke.py [--port 8420] [--timeout 900]
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from typing import Sequence

import ci_common as ci

ASSET_LINK = re.compile(r"""(?:src|href)=["'](/assets/[^"'?#]+)["']""")
STOP_GRACE_S = 30


def ui_problems(html: str) -> tuple[list[str], list[str]]:
    """(problems, /assets/ paths) of the index.html served at '/'."""
    problems = []
    if 'id="root"' not in html:
        problems.append('index.html sin <div id="root">')
    if "/src/main.tsx" in html:
        problems.append("se sirvió el index.html de desarrollo (sin compilar)")
    if "npm run build" in html:
        problems.append("el servidor dice que la interfaz no está compilada (frontend/dist)")
    assets = ASSET_LINK.findall(html)
    if not any(path.endswith(".js") for path in assets):
        problems.append("index.html no enlaza ningún /assets/*.js")
    return problems, assets


def check_ui(base: str, report: ci.Report) -> None:
    status, headers, raw = ci.http_request(f"{base}/", accept="text/html", timeout=30)
    html = raw.decode("utf-8", errors="replace")
    problems, assets = ui_problems(html) if status == 200 else ([f"GET / → HTTP {status}"], [])
    report.expect("interfaz compilada servida en /", problems, headers.get("content-type", ""))
    script = next((path for path in assets if path.endswith(".js")), None)
    if script:
        status, headers, raw = ci.http_request(f"{base}{script}", accept="*/*", timeout=30)
        kind = headers.get("content-type", "")
        report.expect("recurso de la interfaz", [] if status == 200 and raw and "javascript" in kind else
                      [f"GET {script} → HTTP {status} ({kind or 'sin tipo'})"], f"{script} · {len(raw) // 1024} kB")


def stop(proc: subprocess.Popen, report: ci.Report) -> None:
    started = time.monotonic()
    try:
        os.killpg(proc.pid, signal.SIGTERM)  # the whole group: start.sh, and anything it left running
    except ProcessLookupError:
        pass
    try:
        code = proc.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
        report.fail("parada limpia con SIGTERM", f"seguía en marcha tras {STOP_GRACE_S} s (SIGKILL)")
        return
    elapsed = time.monotonic() - started
    # uvicorn re-raises the signal after a graceful shutdown: -15 (SIGTERM) is a clean stop, like 0
    report.expect("parada limpia con SIGTERM", [] if code in (0, -signal.SIGTERM, 128 + signal.SIGTERM) else
                  [f"código de salida {code}"], f"{elapsed:.1f} s, código {code}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Arranca NEXUS con bash start.sh y comprueba que funciona (Linux).")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--timeout", type=float, default=900.0, help="segundos para instalar, compilar y arrancar")
    args = parser.parse_args(argv)
    if os.name == "nt":
        ci.log("launcher_smoke.py es solo para Linux/macOS (en Windows usa .\\start.bat).")
        return 2
    report = ci.Report("Lanzador Linux (start.sh)")
    base = f"http://127.0.0.1:{args.port}"
    cmd = ["bash", "start.sh", "--no-browser", "--port", str(args.port)]
    ci.log("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=ci.ROOT, start_new_session=True, stdin=subprocess.DEVNULL)
    try:
        health = ci.wait_for_health(f"{base}/api/health", timeout_s=args.timeout, interval_s=2.0,
                                    alive=lambda: proc.poll() is None)
        report.expect("/api/health en modo local", ci.check_health(health), ci.services_summary(health))
        check_ui(base, report)
    except ci.CheckFailed as exc:
        report.fail("arranque con start.sh", str(exc))
    except ci.PROBE_ERRORS as exc:
        report.fail("arranque con start.sh", f"{type(exc).__name__}: {exc}")
    finally:
        if proc.poll() is None:
            stop(proc, report)
        else:
            report.fail("start.sh terminó solo", f"código {proc.returncode}")
    return report.finish()


if __name__ == "__main__":
    sys.exit(main())
