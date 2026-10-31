"""Walk-forward validation from the command line (no server): candles straight from the exchanges' public APIs
through the app's providers and `app.util.http_client()`, default NEXUS settings, report as JSON (+ Markdown tables).

    cd backend
    PYTHONUTF8=1 .venv/Scripts/python -m app.validation.cli --symbols BINANCE:BTCUSDT,BINANCE:ETHUSDT \
        --tfs 1h,4h --bars 5000 --out ../docs/validation/report.json --markdown .tmp/validation/tables.md

Exit code 0 when at least one series was validated.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path

from app import config
from app.config import DEFAULT_SETTINGS
from app.util import http_client
from app.validation.data import load_direct
from app.validation.markdown import report_markdown
from app.validation.report import compact_report
from app.validation.runner import DEFAULT_SYMBOLS, DEFAULT_TFS, ValidationRequest, normalize_request, run_validation
from app.validation.walkforward import WFConfig

PAUSE_BETWEEN_SERIES_S = 0.5  # be gentle with the public APIs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m app.validation.cli",
                                 description="Validación walk-forward de NEXUS + filtro (solo simulación).")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="BINANCE:BTCUSDT,BINANCE:ETHUSDT,…")
    ap.add_argument("--tfs", default=",".join(DEFAULT_TFS), help="15m,1h,4h,1d…")
    ap.add_argument("--bars", type=int, default=3000, help="velas cerradas por serie (1000-5000)")
    ap.add_argument("--folds", type=int, default=int(DEFAULT_SETTINGS.get("validation.folds", 5)))
    ap.add_argument("--warmup", type=int, default=int(DEFAULT_SETTINGS.get("validation.warmup_bars", 500)))
    ap.add_argument("--random-runs", type=int, default=int(DEFAULT_SETTINGS.get("validation.random_runs", 1000)))
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="", help="archivo JSON del informe")
    ap.add_argument("--compact", default="", help="archivo JSON con la versión reducida (para docs/validation/)")
    ap.add_argument("--markdown", default="", help="archivo Markdown con las tablas")
    return ap.parse_args(argv)


async def main_async(args: argparse.Namespace) -> dict:
    if config.OFFLINE:
        raise SystemExit("NEXUS_OFFLINE=1: la validación desde la línea de comandos necesita red")
    req = normalize_request(args.symbols, args.tfs, args.bars, ValidationRequest())
    cfg = WFConfig(folds=args.folds, warmup_bars=args.warmup, random_runs=args.random_runs,
                   bootstrap_runs=args.bootstrap, risk_pct=float(DEFAULT_SETTINGS["risk.per_trade_pct"]),
                   slippage_pct=float(DEFAULT_SETTINGS["risk.slippage_pct"]), seed=args.seed)

    def progress(update: dict) -> None:
        if update.get("series") is not None or update.get("error"):
            print(f"[{update.get('done')}/{update.get('total')}] {update.get('message')}", flush=True)

    async with http_client(timeout=30.0) as client:
        async def loader(symbol: str, tf: str, bars: int):
            await asyncio.sleep(PAUSE_BETWEEN_SERIES_S)
            return await load_direct(symbol, tf, bars, client)

        return await run_validation(req, loader, settings=copy.deepcopy(DEFAULT_SETTINGS), config=cfg,
                                    progress=progress, source="cli")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(main_async(args))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"Informe: {path}")
    if args.compact:
        path = Path(args.compact)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(compact_report(report), ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
        print(f"Informe reducido: {path}")
    if args.markdown:
        path = Path(args.markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report_markdown(report), encoding="utf-8")
        print(f"Tablas: {path}")
    print(report["summary"]["text"])
    return 0 if report.get("series") else 1


if __name__ == "__main__":
    sys.exit(main())
