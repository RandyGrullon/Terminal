"""Historical candles for long backtests and research (`app.sim.research`), stored in the local candle table with
source 'import' (kept by maintenance: app.maintenance never prunes them).

- `import_csv(...)`: a CSV of OHLCV bars, for instance exported by QuantDataManager (Dukascopy data: forex, indices,
  metals, stock CFDs), MetaTrader or any other tool. The format is detected: delimiter (, ; tab), header or not,
  one date-time column or a date and a time column, dates as YYYY.MM.DD / YYYY-MM-DD / YYYYMMDD / DD.MM.YYYY, unix
  seconds / ms / µs. Bars are resampled from the file's own timeframe (1m, 5m…) up to TARGET_TFS; a first or last
  bucket the file only covers in part is dropped (no half candles). `tz_offset_h` moves the file's times to UTC.
- `import_binance(...)`: Binance's official public dumps (https://data.binance.vision, monthly zips of klines, spot or
  USDⓈ-M futures for `.P` symbols), one native interval per timeframe, through app.util.http_client (CLAUDE.md rule
  4: an official public endpoint, plain requests). A month that does not exist (404: the pair did not trade yet, or
  the month is not published) is skipped.

Existing bars are never overwritten (a recorded or provider bar wins: ON CONFLICT DO NOTHING). Files are only read
from IMPORT_DIR (data/import), so the API cannot be pointed at any other file of the computer.
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import httpx

from app import config
from app.util import http_client, normalize_tf, now_s, tf_seconds

IMPORT_SOURCE = "import"
IMPORT_DIR = config.DATA_DIR / "import"
TARGET_TFS = ("5m", "15m", "1h", "4h", "1d")
BINANCE_TFS = ("5m", "15m", "1h", "4h", "1d")
BINANCE_DUMPS = "https://data.binance.vision"
BATCH = 5000
MAX_BAD_LINES = 50  # a file with more unreadable lines than this (beyond its header) is refused
INSERT_SQL = ("INSERT INTO candles(symbol, tf, time, open, high, low, close, volume, source) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol, tf, time) DO NOTHING")

Row = tuple[int, float, float, float, float, float]  # (unix s UTC, open, high, low, close, volume)
Progress = Callable[[str], None]


class ImportError_(Exception):
    """A Spanish message for the user (the file cannot be read, the symbol is not valid, offline…)."""


# ----------------------------------------------------------------------------- parsing

_YMD = re.compile(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(?:[ T](.+))?$")
_DMY = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})(?:[ T](.+))?$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:[ T]?(\d{2})(\d{2})(\d{2})?)?$")
_COMPACT_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})[ T](.+)$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?(?:Z|[+-]00:?00)?$")

HEADER_NAMES = {
    "date": "date", "fecha": "date", "day": "date", "<date>": "date", "gmt time": "datetime", "local time": "datetime",
    "time": "time", "hora": "time", "<time>": "time",
    "datetime": "datetime", "date_time": "datetime", "timestamp": "datetime", "open_time": "datetime",
    "open time": "datetime", "dt": "datetime",
    "open": "open", "<open>": "open", "o": "open", "high": "high", "<high>": "high", "h": "high",
    "low": "low", "<low>": "low", "l": "low", "close": "close", "<close>": "close", "c": "close",
    "volume": "volume", "vol": "volume", "<vol>": "volume", "tickvol": "volume", "<tickvol>": "volume",
    "tick_volume": "volume", "tick volume": "volume", "v": "volume",
}


def _hms(text: str | None) -> int | None:
    """Seconds of a time of day: HH:MM[:SS[.fff]] or HHMM[SS]; 0 when there is none."""
    if not text:
        return 0
    t = text.strip()
    if t.isdigit() and len(t) in (4, 6):
        h, mi, s = int(t[:2]), int(t[2:4]), int(t[4:6] or 0)
        return h * 3600 + mi * 60 + s if h < 24 and mi < 60 and s < 61 else None
    m = _TIME.match(t)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return h * 3600 + mi * 60 + s if h < 24 and mi < 60 and s < 61 else None


def _epoch(y: int, mo: int, d: int) -> int | None:
    try:
        return int(datetime(y, mo, d, tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def parse_time(date_text: str, time_text: str | None = None) -> int | None:
    """Unix seconds (as written, before any time-zone offset) of a date (+ time) in the formats above, or None."""
    t = (date_text or "").strip().strip('"')
    if not t:
        return None
    if t.isdigit() and len(t) in (10, 13, 16):  # unix seconds / ms / µs (8, 12 and 14 digits: YYYYMMDD[HHMM[SS]])
        n = int(t)
        return n // 1_000_000 if len(t) == 16 else n // 1000 if len(t) == 13 else n
    for rx, order in ((_YMD, "ymd"), (_DMY, "dmy")):
        m = rx.match(t)
        if m:
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            day = _epoch(y, mo, d)
            secs = _hms(m.group(4) if m.group(4) else time_text)
            return None if day is None or secs is None else day + secs
    m = _COMPACT_DATE.match(t)  # YYYYMMDD and a time after a space
    if m:
        day = _epoch(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        secs = _hms(m.group(4))
        return None if day is None or secs is None else day + secs
    m = _COMPACT.match(t)
    if m:
        day = _epoch(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m.group(4):
            secs = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(6) or 0)
        else:
            secs = _hms(time_text)
        return None if day is None or secs is None else day + secs
    return None


def _num(text: str) -> float | None:
    try:
        v = float(str(text).strip().strip('"').replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _delimiter(sample: str) -> str:
    first = next((ln for ln in sample.splitlines() if ln.strip()), "")
    counts = {d: first.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _layout(cells: list[str]) -> dict[str, int] | None:
    """Column positions from a header row, or None when the row is data."""
    names = [c.strip().strip('"').lower() for c in cells]
    if not any(any(ch.isalpha() for ch in n) for n in names) or all(_num(n) is not None for n in names if n):
        return None
    out: dict[str, int] = {}
    for i, n in enumerate(names):
        key = HEADER_NAMES.get(n)
        if key and key not in out:
            out[key] = i
    return out


def _guess_layout(cells: list[str]) -> dict[str, int]:
    """Positions of a header-less row: date, time, O, H, L, C[, V] when the 2nd cell is a time; else datetime, O..V."""
    if len(cells) >= 6 and _TIME.match(cells[1].strip()):
        return {"date": 0, "time": 1, "open": 2, "high": 3, "low": 4, "close": 5, **({"volume": 6} if len(cells) > 6 else {})}
    return {"datetime": 0, "open": 1, "high": 2, "low": 3, "close": 4, **({"volume": 5} if len(cells) > 5 else {})}


def parse_csv(lines: Iterable[str], *, tz_offset_h: float = 0.0, stats: dict | None = None) -> Iterator[Row]:
    """Bars of a CSV (any of the formats above), ascending as in the file; unreadable lines are counted in
    stats['bad'] (ImportError_ past MAX_BAD_LINES)."""
    it = iter(lines)
    head: list[str] = []
    for ln in it:
        if ln.strip():
            head.append(ln)
            break
    if not head:
        return
    delim = _delimiter(head[0])
    offset = int(round(tz_offset_h * 3600))
    st = stats if stats is not None else {}
    st.setdefault("bad", 0)
    st.setdefault("read", 0)

    def rows() -> Iterator[list[str]]:
        yield from csv.reader(head, delimiter=delim)
        yield from csv.reader(it, delimiter=delim)

    layout: dict[str, int] | None = None
    for cells in rows():
        if not cells or not any(c.strip() for c in cells):
            continue
        if layout is None:
            layout = _layout(cells)
            if layout is not None:
                if not ({"open", "high", "low", "close"} <= layout.keys()) or not (
                        "datetime" in layout or "date" in layout):
                    raise ImportError_("No reconozco las columnas del archivo: hacen falta fecha (y hora), apertura, "
                                       "máximo, mínimo y cierre.")
                continue
            layout = _guess_layout(cells)
        try:
            if "datetime" in layout:
                ts = parse_time(cells[layout["datetime"]])
            else:
                ts = parse_time(cells[layout["date"]], cells[layout["time"]] if "time" in layout else None)
            o, h, lo, c = (_num(cells[layout[k]]) for k in ("open", "high", "low", "close"))
            v = _num(cells[layout["volume"]]) if "volume" in layout and layout["volume"] < len(cells) else 0.0
        except IndexError:
            ts = None
            o = h = lo = c = v = None
        if ts is None or None in (o, h, lo, c) or min(o, h, lo, c) <= 0:  # type: ignore[type-var]
            st["bad"] += 1
            if st["bad"] > MAX_BAD_LINES:
                raise ImportError_(f"Más de {MAX_BAD_LINES} líneas ilegibles: revisa el formato del archivo (fecha, "
                                   "hora y precios).")
            continue
        st["read"] += 1
        hi = max(h, o, c, lo)  # type: ignore[type-var]
        low = min(lo, o, c, h)  # type: ignore[type-var]
        yield (ts - offset, float(o), float(hi), float(low), float(c), float(v or 0.0))  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- resampling


class Resampler:
    """Streaming OHLCV resampling of ascending bars into one timeframe; `flush()` closes the last bucket. A bucket the
    data only covers in part (the first one starting late, the last one ending early) is dropped."""

    def __init__(self, tf: str, src_s: int):
        self.tf = tf
        self.sec = tf_seconds(tf)
        self.src_s = src_s
        self.cur: list[float] | None = None  # [start, o, h, l, c, v, first_ts, last_ts]
        self.first = True

    def _bucket(self, ts: int) -> int:
        if self.tf == "1w":
            return ts - ((ts // 86400 + 3) % 7) * 86400 - ts % 86400  # Monday 00:00 UTC
        return ts - ts % self.sec

    def _emit(self, done: bool) -> tuple | None:
        b = self.cur
        if b is None:
            return None
        start = int(b[0])
        partial_head = self.first and int(b[6]) > start
        partial_tail = done and int(b[7]) + self.src_s < start + self.sec
        self.first = False
        if partial_head or partial_tail:
            return None
        return (start, b[1], b[2], b[3], b[4], b[5])

    def add(self, row: Row) -> tuple | None:
        ts, o, h, lo, c, v = row
        start = self._bucket(ts)
        out = None
        if self.cur is not None and start != self.cur[0]:
            if start < self.cur[0]:
                return None  # an older bar after a newer one: ignored
            out = self._emit(False)
            self.cur = None
        if self.cur is None:
            self.cur = [start, o, h, lo, c, v, ts, ts]
        else:
            b = self.cur
            b[2] = max(b[2], h)
            b[3] = min(b[3], lo)
            b[4] = c
            b[5] += v
            b[7] = ts
        return out

    def flush(self) -> tuple | None:
        out = self._emit(True)
        self.cur = None
        return out


def source_seconds(times: list[int]) -> int:
    """The data's own bar size: the smallest positive gap among its first bars (60 for minute data)."""
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    return min(gaps) if gaps else 60


# ----------------------------------------------------------------------------- storage


async def _store(db: Any, symbol: str, tf: str, bars: list[tuple]) -> None:
    if bars:
        await db.executemany(INSERT_SQL, [(symbol, tf, int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]),
                                            float(b[5]), IMPORT_SOURCE) for b in bars])


async def count_imported(db: Any, symbol: str) -> dict[str, dict[str, Any]]:
    """tf -> {bars, from, to} of the imported candles of a symbol."""
    rows = await db.fetchall(
        "SELECT tf, COUNT(*) AS n, MIN(time) AS t0, MAX(time) AS t1 FROM candles WHERE symbol = ? AND source = ? "
        "GROUP BY tf", (symbol, IMPORT_SOURCE))
    return {r["tf"]: {"bars": int(r["n"]), "from": int(r["t0"]), "to": int(r["t1"])} for r in rows}


def resolve_import_path(name: str) -> Path:
    """A file inside IMPORT_DIR (a bare name or a path under it); ImportError_ for anything else."""
    raw = str(name or "").strip().strip('"')
    if not raw:
        raise ImportError_("Falta el archivo: ponlo en la carpeta data/import y escribe su nombre.")
    base = IMPORT_DIR.resolve()
    p = Path(raw)
    p = (p if p.is_absolute() else base / p).resolve()
    if base != p and base not in p.parents:
        raise ImportError_(f"Solo se importan archivos de la carpeta {base}.")
    if not p.is_file():
        raise ImportError_(f"No encuentro {p.name} en {base}.")
    return p


async def import_csv(db: Any, path: Path, symbol: str, *, tz_offset_h: float = 0.0,
                     tfs: Iterable[str] = TARGET_TFS, progress: Progress | None = None) -> dict[str, Any]:
    """Import a CSV of bars as `symbol` (resampled to every tf of `tfs` not finer than the file). The file is read in
    a worker thread; the inserts go in batches."""
    symbol = str(symbol or "").strip().upper()
    if not symbol or ":" not in symbol:
        raise ImportError_("El símbolo debe llevar su mercado, por ejemplo OANDA:SPX500USD o FX:EURUSD.")
    targets = [normalize_tf(t) for t in tfs]
    stats: dict[str, Any] = {"bad": 0, "read": 0}

    def read_all() -> list[Row]:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            return list(parse_csv(fh, tz_offset_h=tz_offset_h, stats=stats))

    rows = await asyncio.to_thread(read_all)
    if not rows:
        raise ImportError_("El archivo no tiene velas legibles.")
    rows.sort(key=lambda r: r[0])
    src = source_seconds([r[0] for r in rows[:500]])
    usable = [tf for tf in targets if tf_seconds(tf) >= src and tf_seconds(tf) % src == 0]
    if not usable:
        raise ImportError_(f"El archivo tiene velas de {src // 60} min: ninguna temporalidad pedida es múltiplo de ellas.")
    out: dict[str, int] = {}
    for tf in usable:
        rs = Resampler(tf, src)
        buf: list[tuple] = []
        n = 0
        for r in rows:
            bar = rs.add(r)
            if bar is not None:
                buf.append(bar)
                if len(buf) >= BATCH:
                    await _store(db, symbol, tf, buf)
                    n += len(buf)
                    buf = []
        last = rs.flush()
        if last is not None:
            buf.append(last)
        await _store(db, symbol, tf, buf)
        n += len(buf)
        out[tf] = n
        if progress:
            progress(f"{symbol} {tf}: {n} velas")
    return {"symbol": symbol, "file": path.name, "lines_read": int(stats["read"]), "lines_skipped": int(stats["bad"]),
            "source_tf_s": src, "from": rows[0][0], "to": rows[-1][0], "bars": out,
            "stored": await count_imported(db, symbol)}


# ----------------------------------------------------------------------------- Binance public dumps


def binance_target(symbol: str) -> tuple[str, str]:
    """(pair, market path) of a Binance symbol: BINANCE:BTCUSDT -> ("BTCUSDT", "spot"); BTCUSDT.P -> "futures/um"."""
    sym = str(symbol or "").strip().upper()
    exchange, _, ticker = sym.partition(":")
    if not ticker:
        exchange, ticker = "BINANCE", exchange
    if exchange != "BINANCE":
        raise ImportError_(f"{sym}: los volcados oficiales de Binance solo sirven símbolos BINANCE:…")
    perp = ticker.endswith(".P")
    pair = ticker[:-2] if perp else ticker
    if not pair.isalnum():
        raise ImportError_(f"{sym}: símbolo no válido.")
    return pair, ("futures/um" if perp else "spot")


def parse_kline_csv(text: str) -> list[Row]:
    """Rows of a Binance klines CSV (open_time in ms, or µs in recent spot files; an optional header)."""
    out: list[Row] = []
    for cells in csv.reader(io.StringIO(text)):
        if len(cells) < 6 or not cells[0].strip().isdigit():
            continue
        t = int(cells[0])
        ts = t // 1_000_000 if t > 10**14 else t // 1000
        try:
            o, h, lo, c, v = (float(x) for x in cells[1:6])
        except ValueError:
            continue
        if min(o, h, lo, c) > 0:
            out.append((ts, o, h, lo, c, v))
    return out


def months_back(months: int, now: int | None = None) -> list[tuple[int, int]]:
    """The last `months` complete months, oldest first (the current month has no monthly file yet)."""
    d = datetime.fromtimestamp(int(now if now is not None else now_s()), tz=timezone.utc)
    y, m = d.year, d.month
    out: list[tuple[int, int]] = []
    for _ in range(max(int(months), 0)):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append((y, m))
    return list(reversed(out))


async def import_binance(db: Any, symbol: str, *, months: int = 36, tfs: Iterable[str] = BINANCE_TFS,
                         progress: Progress | None = None) -> dict[str, Any]:
    """Download the official monthly kline dumps of `symbol` for each tf and store them as imported candles."""
    if config.OFFLINE:
        raise ImportError_("Modo sin conexión (NEXUS_OFFLINE): no se descarga nada.")
    pair, market = binance_target(symbol)
    sym = str(symbol).strip().upper()
    sym = sym if ":" in sym else f"BINANCE:{sym}"
    wanted = [normalize_tf(t) for t in tfs]
    out: dict[str, int] = {}
    missing = 0
    async with http_client(timeout=60.0) as client:
        for tf in wanted:
            n = 0
            for y, m in months_back(months):
                url = f"{BINANCE_DUMPS}/data/{market}/monthly/klines/{pair}/{tf}/{pair}-{tf}-{y:04d}-{m:02d}.zip"
                try:
                    r = await client.get(url, follow_redirects=False)
                except httpx.HTTPError as exc:
                    raise ImportError_(f"No se pudo descargar de data.binance.vision ({type(exc).__name__}).") from None
                if r.status_code == 404:
                    missing += 1
                    continue
                if r.status_code >= 300:
                    raise ImportError_(f"data.binance.vision respondió {r.status_code} para {pair} {tf} {y}-{m:02d}.")
                try:
                    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                        text = "".join(zf.read(name).decode("utf-8", "replace") for name in zf.namelist()
                                       if name.endswith(".csv"))
                except zipfile.BadZipFile:
                    raise ImportError_(f"El archivo de {pair} {tf} {y}-{m:02d} llegó dañado.") from None
                rows = parse_kline_csv(text)
                await _store(db, sym, tf, [(t, o, h, lo, c, v) for (t, o, h, lo, c, v) in rows])
                n += len(rows)
            out[tf] = n
            if progress:
                progress(f"{sym} {tf}: {n} velas")
    return {"symbol": sym, "months": int(months), "missing_months": missing, "bars": out,
            "stored": await count_imported(db, sym)}
