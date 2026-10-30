"""Samples and test doubles for the MACRO tests (semáforo de mercado).

(No tests in this module; it is named test_macro_* only to stay inside the MACRO-BE file ownership.)
The samples are real responses captured once from the live sources on 2026-10-30 (trimmed).
"""
from __future__ import annotations

from typing import Any

# ForexFactory weekly feed (week of 2026-10-25), 18 of its 98 events.
FF_SAMPLE: list[dict] = [
    {"title": "Bank Holiday", "country": "NZD", "date": "2026-10-25T15:00:00-04:00", "impact": "Holiday", "forecast": "", "previous": ""},
    {"title": "CB Consumer Confidence", "country": "USD", "date": "2026-10-27T10:00:00-04:00", "impact": "Medium", "forecast": "81.2", "previous": "81.9"},
    {"title": "CPI m/m", "country": "AUD", "date": "2026-10-27T20:30:00-04:00", "impact": "High", "forecast": "0.7%", "previous": "0.4%"},
    {"title": "Official Cash Rate", "country": "NZD", "date": "2026-10-27T21:00:00-04:00", "impact": "High", "forecast": "3.00%", "previous": "2.75%"},
    {"title": "Overnight Rate", "country": "CAD", "date": "2026-10-28T09:45:00-04:00", "impact": "High", "forecast": "2.50%", "previous": "2.25%"},
    {"title": "Crude Oil Inventories", "country": "USD", "date": "2026-10-28T10:30:00-04:00", "impact": "Low", "forecast": "0.1M", "previous": "-3.1M"},
    {"title": "Federal Funds Rate", "country": "USD", "date": "2026-10-28T14:00:00-04:00", "impact": "High", "forecast": "4.00%", "previous": "4.00%"},
    {"title": "FOMC Statement", "country": "USD", "date": "2026-10-28T14:00:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "FOMC Press Conference", "country": "USD", "date": "2026-10-28T14:30:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "Advance GDP q/q", "country": "USD", "date": "2026-10-29T08:30:00-04:00", "impact": "High", "forecast": "3.2%", "previous": "1.5%"},
    {"title": "Core PCE Price Index m/m", "country": "USD", "date": "2026-10-29T08:30:00-04:00", "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
    {"title": "Unemployment Claims", "country": "USD", "date": "2026-10-29T08:30:00-04:00", "impact": "Medium", "forecast": "198K", "previous": "194K"},
    {"title": "Main Refinancing Rate", "country": "EUR", "date": "2026-10-29T09:15:00-04:00", "impact": "High", "forecast": "2.65%", "previous": "2.65%"},
    {"title": "ECB Press Conference", "country": "EUR", "date": "2026-10-29T09:45:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "BOJ Policy Rate", "country": "JPY", "date": "2026-10-29T23:02:00-04:00", "impact": "High", "forecast": "<1.25%", "previous": "<1.25%"},
    {"title": "German Prelim CPI m/m", "country": "EUR", "date": "2026-10-30T03:29:00-04:00", "impact": "Medium", "forecast": "0.1%", "previous": "0.6%"},
    {"title": "Employment Cost Index q/q", "country": "USD", "date": "2026-10-30T08:30:00-04:00", "impact": "Medium", "forecast": "0.8%", "previous": "0.9%"},
    {"title": "FOMC Member Daly Speaks", "country": "USD", "date": "2026-10-30T12:00:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
]

FNG_SAMPLE = {
    "name": "Fear and Greed Index",
    "data": [{"value": "30", "value_classification": "Fear", "timestamp": "1793318400", "time_until_update": "17997"}],
    "metadata": {"error": None},
}

OKX_SAMPLE = {
    "code": "0", "msg": "",
    "data": [{
        "formulaType": "withRate", "fundingRate": "0.0001000000000000", "fundingTime": "1793404800000",
        "instId": "BTC-USDT-SWAP", "instType": "SWAP", "interestRate": "0.0001000000000000", "method": "current_period",
        "nextFundingRate": "", "nextFundingTime": "1793433600000", "premium": "-0.0002437882874719",
        "prevFundingTime": "1793376000000", "settFundingRate": "0.0001000000000000", "settState": "settled",
        "ts": "1793386744780",
    }],
}

# rt.hub.tv_scan("global", ["close", "change", "change_abs", "high", "low"], tickers=[...]) on 2026-10-30.
TV_ROWS: list[dict] = [
    {"symbol": "TVC:VIX", "close": 22.29, "change": 29.668411867364732, "change_abs": 5.1, "high": 23.93, "low": 18.03},
    {"symbol": "CME_MINI:ES1!", "close": 7568, "change": -2.4019086307508783, "change_abs": -186.25, "high": 7713, "low": 7560},
    {"symbol": "CME_MINI:NQ1!", "close": 28293.5, "change": -4.2909816656518505, "change_abs": -1268.5, "high": 29180, "low": 28241},
    {"symbol": "TVC:DXY", "close": 101.889, "change": 0.03534505611027791, "change_abs": 0.036, "high": 102.089, "low": 101.544},
    {"symbol": "TVC:US10Y", "close": 4.929, "change": -0.1620417257443793, "change_abs": -0.008, "high": 4.958, "low": 4.892},
    {"symbol": "TVC:US02Y", "close": 4.575, "change": -0.5867014341590642, "change_abs": -0.027, "high": 4.627, "low": 4.554},
    {"symbol": "TVC:MOVE", "close": 100.2131, "change": 7.124218988011559, "change_abs": 6.6646, "high": 100.2131, "low": 97.0271},
    {"symbol": "NYMEX:CL1!", "close": 103.97, "change": 1.9313725490196068, "change_abs": 1.97, "high": 104.99, "low": 102.43},
    {"symbol": "COMEX:GC1!", "close": 3893, "change": -3.433050553157714, "change_abs": -138.4, "high": 4038.2, "low": 3885.9},
    {"symbol": "CRYPTOCAP:BTC.D", "close": 60.1734219318592, "change": -0.5576029159657827, "change_abs": -0.3374, "high": 60.606, "low": 60.1734},
]


def calm_quotes() -> dict[str, dict]:
    """A quiet session: every metric far from its thresholds."""
    return {
        "vix": {"value": 15.2, "change_pct": 1.5, "change_abs": 0.22},
        "es": {"value": 6800.0, "change_pct": 0.3, "change_abs": 20.0},
        "nq": {"value": 25000.0, "change_pct": 0.4, "change_abs": 100.0},
        "dxy": {"value": 99.5, "change_pct": 0.1, "change_abs": 0.1},
        "us10y": {"value": 4.2, "change_pct": 0.5, "change_abs": 0.02},
        "move": {"value": 90.0, "change_pct": -1.0, "change_abs": -0.9},
        "oil": {"value": 70.0, "change_pct": 0.5, "change_abs": 0.35},
        "btc_d": {"value": 58.0, "change_pct": 0.1, "change_abs": 0.05},
    }


def metrics_with(now_s: int, quotes: dict | None = None, fng: int | None = 50, funding: float | None = 0.0001) -> dict:
    """Raw metrics snapshot (as MetricsCollector.snapshot) fresh at `now_s`."""
    return {
        "quotes": quotes if quotes is not None else calm_quotes(),
        "quotes_at": now_s * 1000,
        "fng": {"value": fng, "classification": "", "time": now_s} if fng is not None else None,
        "fng_at": now_s * 1000,
        "funding": {"BTC": {"rate": funding, "interval_h": 8.0, "time": now_s * 1000}} if funding is not None else {},
        "funding_at": now_s * 1000,
    }


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if self.text is not None:
            raise ValueError("not json")
        return self._payload


class FakeHTTP:
    """Stand-in for app.util.http_client(): routes GETs by URL substring to canned responses."""

    def __init__(self, routes: dict[str, FakeResponse | Exception]):
        self.routes = routes
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, **_kw: Any) -> "FakeHTTP":
        return self

    async def __aenter__(self) -> "FakeHTTP":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get(self, url: str, params: dict | None = None) -> FakeResponse:
        self.calls.append((url, params))
        for key, resp in self.routes.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return FakeResponse(404, {})


class FakeHub:
    """Minimal MarketDataHub: tv_scan returns the captured rows; symbol_info answers from a dict."""

    def __init__(self, rows: list[dict] | None = None, infos: dict[str, dict] | None = None, fail: bool = False):
        self.rows = list(TV_ROWS if rows is None else rows)
        self.infos = infos or {}
        self.fail = fail
        self.scans: list[tuple[str, list[str], list[str] | None]] = []
        self.info_calls: list[str] = []

    async def tv_scan(self, market: str, columns: list[str], *, tickers: list[str] | None = None, **_kw: Any) -> dict:
        self.scans.append((market, list(columns), tickers))
        if self.fail:
            raise RuntimeError("TradingView no responde")
        wanted = set(tickers or [])
        rows = [r for r in self.rows if not wanted or r["symbol"] in wanted]
        return {"total": len(rows), "rows": rows}

    async def symbol_info(self, symbol: str) -> dict:
        self.info_calls.append(symbol)
        return self.infos.get(symbol, {"symbol": symbol})

    async def resolve(self, text: str) -> str | None:
        return {"btc": "BINANCE:BTCUSDT", "eurusd": "FX:EURUSD"}.get(text.lower())


class Clock:
    """Mutable fake clock usable as both wall clock and monotonic clock."""

    def __init__(self, t: float):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
