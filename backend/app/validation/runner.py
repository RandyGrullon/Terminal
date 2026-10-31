"""One validation job over several (symbol, tf) series, shared by ValidationService (candles from rt.hub) and the
CLI (candles straight from the exchanges' public APIs). The CPU work of each series runs in a worker thread."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.data.base import CandleSet
from app.sim.common import fmt_num
from app.util import normalize_tf, now_ms
from app.validation.data import MAX_BARS, MIN_BARS
from app.validation.report import build_report
from app.validation.verdict import ci_for, count_text, r2
from app.validation.walkforward import ValidationDataError, WFConfig, validate_series

MAX_SERIES = 24  # symbols × timeframes per job
MAX_SYMBOLS = 12
DEFAULT_SYMBOLS = ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "BINANCE:SOLUSDT", "BINANCE:XRPUSDT", "BINANCE:BNBUSDT"]
DEFAULT_TFS = ["1h", "4h"]

Loader = Callable[[str, str, int], Awaitable[CandleSet]]
Progress = Callable[[dict[str, Any]], None]


class ValidationInputError(ValueError):
    """Invalid validation request (Spanish message, HTTP 400)."""


@dataclass
class ValidationRequest:
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    tfs: list[str] = field(default_factory=lambda: list(DEFAULT_TFS))
    bars: int = 3000

    def to_dict(self) -> dict[str, Any]:
        return {"symbols": list(self.symbols), "tfs": list(self.tfs), "bars": int(self.bars)}


def split_list(value: Any) -> list[str]:
    """'a,b c' / ['a', 'b,c'] -> ['a', 'b', 'c'] (empty items dropped)."""
    items = value if isinstance(value, (list, tuple)) else [value] if value is not None else []
    out: list[str] = []
    for item in items:
        for part in str(item).replace(";", ",").replace(" ", ",").split(","):
            if part.strip():
                out.append(part.strip())
    return out


def normalize_request(symbols: Any, tfs: Any, bars: Any, defaults: ValidationRequest) -> ValidationRequest:
    """Validated request (symbols NOT resolved yet). Raises ValidationInputError."""
    syms = [s.upper() for s in split_list(symbols)] or list(defaults.symbols)
    syms = list(dict.fromkeys(syms))
    if len(syms) > MAX_SYMBOLS:
        raise ValidationInputError(f"Demasiados símbolos: {len(syms)} (máximo {MAX_SYMBOLS})")
    out_tfs: list[str] = []
    for raw in split_list(tfs) or list(defaults.tfs):
        try:
            tf = normalize_tf(raw)
        except ValueError as exc:
            raise ValidationInputError(str(exc)) from None
        if tf not in out_tfs:
            out_tfs.append(tf)
    if len(syms) * len(out_tfs) > MAX_SERIES:
        raise ValidationInputError(f"Demasiadas series: {len(syms)} símbolos × {len(out_tfs)} temporalidades "
                                   f"(máximo {MAX_SERIES})")
    if bars is None or bars == "":
        n = int(defaults.bars)
    else:
        try:
            n = int(float(bars))
        except (TypeError, ValueError, OverflowError):
            raise ValidationInputError("El número de velas debe ser un entero") from None
    if not MIN_BARS <= n <= MAX_BARS:
        raise ValidationInputError(f"El número de velas debe estar entre {MIN_BARS} y {MAX_BARS}")
    return ValidationRequest(syms, out_tfs, n)


def series_line(res: dict) -> str:
    """One Spanish line per validated series (events, terminal, CLI)."""
    o = res["overall"]["nexus"]
    rnd = res["overall"]["random"]
    pct = rnd.get("percentile")
    pct_txt = f" · percentil frente al azar {fmt_num(pct, 0)}" if pct is not None else ""
    ci = ci_for(o.get("ci95"), o["trades"])
    ci_txt = ci if ci.startswith("sin intervalo") else f"IC 95 % {ci}"
    return (f"Validación {res['symbol']} {res['tf']}: {count_text(o['trades'], 'operación', 'operaciones')} · "
            f"expectativa {r2(o.get('expectancy_r'))} ({ci_txt}){pct_txt} · {res['verdict']['label']}")


async def run_validation(request: ValidationRequest, loader: Loader, *, settings: dict, config: WFConfig,
                         progress: Progress | None = None, source: str = "app") -> dict[str, Any]:
    """Validate every (symbol, tf) of the request and build the report. Series that cannot be validated are listed
    in `errors` (Spanish message) and never stop the job."""
    started = now_ms()
    series: list[dict] = []
    errors: list[dict] = []
    pairs = [(s, tf) for s in request.symbols for tf in request.tfs]

    def report(**kw: Any) -> None:
        if progress is not None:
            try:
                progress(kw)
            except Exception:  # a progress listener never stops the job
                pass

    for i, (symbol, tf) in enumerate(pairs):
        label = f"{symbol} {tf}"
        report(done=i, total=len(pairs), current=label, message=f"Descargando velas de {label}…")
        try:
            cs = await loader(symbol, tf, request.bars)
            report(done=i, total=len(pairs), current=label, message=f"Validando {label} ({len(cs)} velas)…")
            res = await asyncio.to_thread(validate_series, cs, settings, config)
            series.append(res)
            report(done=i + 1, total=len(pairs), current=label, message=series_line(res), series=res)
        except asyncio.CancelledError:
            raise
        except ValidationDataError as exc:
            errors.append({"symbol": symbol, "tf": tf, "message": str(exc)})
            report(done=i + 1, total=len(pairs), current=label, message=f"{label}: {exc}", error=str(exc))
        except Exception as exc:  # never let one series kill the job
            msg = f"Error inesperado ({type(exc).__name__}: {exc})"
            errors.append({"symbol": symbol, "tf": tf, "message": msg})
            report(done=i + 1, total=len(pairs), current=label, message=f"{label}: {msg}", error=msg)
    return build_report(series, errors, request=request.to_dict(), config=config, started_at=started,
                        finished_at=now_ms(), source=source)
