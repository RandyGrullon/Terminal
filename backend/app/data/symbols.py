"""Symbol knowledge (pure functions, no I/O).

- Parsing / validation of TradingView symbols ("EXCHANGE:TICKER").
- Crypto pair splitting and routing of any TradingView crypto symbol to an official exchange API (native pair or an
  equivalent pair: perpetuals ".P", futures and venues without a public candle API map to BINANCE/OKX/KUCOIN).
- Asset-class / scanner-market inference, mapping to keyed providers (Twelve Data, Alpha Vantage, Polygon).
- Aliases for `resolve()` ("btc" -> BINANCE:BTCUSDT, "oro" -> OANDA:XAUUSD ...), DEX/junk detection and the ranking
  used by the symbol search, plus the Spanish names of the TradingView markets.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from app.util import parse_symbol

SIM_EXCHANGE = "SIM"

# ----------------------------------------------------------------------------- exchanges

# Exchanges whose official public candle API we call directly (TradingView exchange -> provider id).
DIRECT_CEX: dict[str, str] = {
    "BINANCE": "binance",
    "OKX": "okx",
    "COINBASE": "coinbase",
    "KRAKEN": "kraken",
    "KUCOIN": "kucoin",
}
PROVIDER_EXCHANGE: dict[str, str] = {v: k for k, v in DIRECT_CEX.items()}
CEX_PROVIDERS: tuple[str, ...] = ("binance", "okx", "coinbase", "kraken", "kucoin")

# Other centralized crypto venues listed on TradingView (served through an equivalent pair).
CRYPTO_EXCHANGES: frozenset[str] = frozenset({
    "BYBIT", "BITSTAMP", "BITFINEX", "GEMINI", "MEXC", "GATEIO", "GATE", "HTX", "HUOBI", "BITGET", "CRYPTOCOM",
    "POLONIEX", "WEEX", "BINGX", "BITMEX", "DERIBIT", "PHEMEX", "UPBIT", "BITHUMB", "BLOFIN", "COINEX", "BITMART",
    "LBANK", "WHITEBIT", "TOOBIT", "PIONEX", "XTCOM", "BTSE", "BYDFI", "BITUNIX", "DEEPCOIN", "BINANCEUS", "OKCOIN",
    "BITVAVO", "BITPANDA", "BITTREX", "EXMO", "PROBIT", "ASCENDEX", "BITRUE", "DELTA", "BITFLYER", "COINCHECK",
    "INDODAX", "BITSO", "FOXBIT", "NOVADAX", "BTCTURK", "PARIBU", "WAZIRX", "COINDCX", "KORBIT", "COINONE", "LUNO",
    "VALR", "CEXIO", "HITBTC", "BIGONE", "DIGIFINEX", "HASHKEY", "ONETRADING", "BULLISH", "BACKPACK", "HYPERLIQUID",
    "WOONETWORK", "CRYPTO", "COINBASEINTX", "BITKUB", "ZAIF", "BITBANK", "MERCADO", "ICRYPEX", "BITGETFUTURES",
})
FOREX_EXCHANGES: frozenset[str] = frozenset({
    "FX", "FX_IDC", "OANDA", "FOREXCOM", "FXCM", "SAXO", "PEPPERSTONE", "ICMARKETS", "EASYMARKETS", "EIGHTCAP",
    "BLACKBULL", "VANTAGE", "FXOPEN", "CAPITALCOM", "CURRENCYCOM", "ACTIVTRADES", "IBKR", "THINKMARKETS", "FPMARKETS",
})
FUTURES_EXCHANGES: frozenset[str] = frozenset({
    "CME", "CME_MINI", "COMEX", "NYMEX", "CBOT", "CBOT_MINI", "COMEX_MINI", "NYMEX_MINI", "ICEUS", "ICEEUR", "ICEENDEX",
    "EUREX", "SGX", "OSE", "MOEX_FUT", "B3", "MEXDER", "TOCOM", "ASX24", "NSEIX", "MCX", "NCDEX",
})
US_STOCK_EXCHANGES: frozenset[str] = frozenset({"NASDAQ", "NYSE", "AMEX", "OTC", "CBOE", "BATS", "NYSEARCA", "ARCA"})
INDEX_EXCHANGES: frozenset[str] = frozenset({"SP", "DJ", "CBOE", "TVC", "INDEX", "CRYPTOCAP", "NSEINDEX"})

# TradingView exchange -> scanner market (used to look tickers up and to fill SymbolInfo.market).
_EXCHANGE_MARKET: dict[str, str] = {
    "NASDAQ": "america", "NYSE": "america", "AMEX": "america", "OTC": "america", "CBOE": "america",
    "BATS": "america", "SP": "america", "DJ": "america", "NYSEARCA": "america", "ARCA": "america",
    "BMV": "mexico", "BIVA": "mexico", "BME": "spain", "BMFBOVESPA": "brazil", "BOVESPA": "brazil",
    "BCBA": "argentina", "BYMA": "argentina", "BVC": "colombia", "BCS": "chile", "BVL": "peru",
    "LSE": "uk", "LSIN": "uk", "AQUIS": "uk", "XETR": "germany", "FWB": "germany", "SWB": "germany",
    "GETTEX": "germany", "TRADEGATE": "germany", "MIL": "italy", "EURONEXT": "france", "TSX": "canada",
    "TSXV": "canada", "CSE": "canada", "NEO": "canada", "TSE": "japan", "NSE": "india", "BSE": "india",
    "HKEX": "hongkong", "ASX": "australia", "SSE": "china", "SZSE": "china", "KRX": "korea", "SIX": "switzerland",
    "TVC": "cfd", "CAPITALCOM": "cfd", "CURRENCYCOM": "cfd", "PEPPERSTONE": "cfd", "EIGHTCAP": "cfd",
}

# Preferred venues for the search ranking (lower index = more relevant).
PREFERRED_VENUES: tuple[str, ...] = (
    "NASDAQ", "NYSE", "AMEX", "BINANCE", "COINBASE", "KRAKEN", "OKX", "KUCOIN", "BYBIT", "BITSTAMP", "FX", "OANDA",
    "FX_IDC", "TVC", "SP", "DJ", "CBOE", "CME", "CME_MINI", "COMEX", "NYMEX", "CBOT", "ICEUS", "ICEEUR", "EUREX",
    "BMV", "BME", "BMFBOVESPA", "BCBA", "BVC", "BCS", "BVL", "LSE", "XETR", "EURONEXT", "MIL", "SIX", "TSX", "ASX",
    "HKEX", "TSE", "NSE", "BSE", "SSE", "SZSE", "KRX", "CRYPTO", "CRYPTOCAP", "INDEX",
)
_VENUE_RANK = {v: i for i, v in enumerate(PREFERRED_VENUES)}

_DEX_MARKERS: tuple[str, ...] = (
    "SWAP", "UNISWAP", "PANCAKE", "CURVE", "SUSHI", "RAYDIUM", "ORCA", "AERODROME", "VELODROME", "PULSEX",
    "TRADERJOE", "BALANCER", "CAMELOT", "METEORA", "QUICKSWAP", "THENA", "KYBER", "DODO", "CETUS", "BLUEFIN",
    "STONFI", "DEDUST", "OSMOSIS", "ASTROPORT", "PUMPFUN", "LFJ", "MAVERICK", "VVSFINANCE", "WOOFI", "PROJECTX",
    "ALIENBASE", "BEETS", "RAMSES", "PHARAOH", "LYNEX", "BLACKHOLE", "SHADOWEXCHANGE", "UNICHAIN", "PHOTON",
)
_DEX_TICKER = re.compile(r"_(?=[0-9A-Z]*\d)[0-9A-Z]{4,}(\.[A-Z]{1,6})?$")

# Quote currencies recognised when splitting crypto tickers (longest first). BUSD and TUSD are deliberately
# omitted: "ARBUSD" must split as ARB/USD and "DOTUSD" / "XBTUSD" as DOT/USD / BTC/USD (TradingView's `currency`
# hint still identifies real TUSD pairs).
KNOWN_QUOTES: tuple[str, ...] = (
    "FDUSD", "USDT", "USDC", "USDE", "DAI", "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "TRY", "BRL",
    "MXN", "ARS", "PLN", "ZAR", "UAH", "IDR", "KRW", "BTC", "ETH", "BNB",
)
_BASE_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}
_EXPIRY = re.compile(r"(\d{2}[FGHJKMNQUVXZ]\d{4}|[FGHJKMNQUVXZ]\d{4}|\d{6})$")

FIAT_CODES: frozenset[str] = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "MXN", "BRL", "ARS", "COP", "CLP", "PEN", "DOP", "CNY",
    "CNH", "HKD", "SGD", "SEK", "NOK", "DKK", "PLN", "HUF", "CZK", "TRY", "ZAR", "INR", "KRW", "IDR", "THB", "ILS",
    "RUB", "SAR", "AED", "TWD", "PHP", "MYR", "VND", "UYU", "CRC", "GTQ", "HNL", "NGN", "EGP", "KES", "RON",
})
METAL_CODES: frozenset[str] = frozenset({"XAU", "XAG", "XPT", "XPD"})

_SYMBOL_RE = re.compile(r"^[A-Z0-9_]{1,32}:[A-Z0-9][A-Z0-9_.!&+\-/^]{0,63}$")

# ----------------------------------------------------------------------------- markets (Spanish names)

MARKETS: tuple[tuple[str, str], ...] = (
    ("global", "Global"), ("america", "EE. UU."), ("crypto", "Cripto"), ("forex", "Forex"), ("futures", "Futuros"),
    ("cfd", "CFD"), ("bond", "Bonos"), ("coin", "Monedas"), ("mexico", "México"), ("spain", "España"),
    ("brazil", "Brasil"), ("argentina", "Argentina"), ("colombia", "Colombia"), ("chile", "Chile"), ("peru", "Perú"),
    ("uk", "Reino Unido"), ("germany", "Alemania"), ("france", "Francia"), ("italy", "Italia"), ("canada", "Canadá"),
    ("japan", "Japón"), ("india", "India"), ("hongkong", "Hong Kong"), ("australia", "Australia"),
    ("china", "China"), ("korea", "Corea del Sur"), ("switzerland", "Suiza"), ("netherlands", "Países Bajos"),
)
MARKET_NAMES: dict[str, str] = dict(MARKETS)
_MARKET_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")


def valid_market(market: str | None) -> bool:
    return bool(market) and bool(_MARKET_RE.match(str(market)))


# ----------------------------------------------------------------------------- aliases (resolve)

_CRYPTO_ALIASES = {
    "btc": "BTC", "bitcoin": "BTC", "xbt": "BTC", "eth": "ETH", "ether": "ETH", "ethereum": "ETH", "sol": "SOL",
    "solana": "SOL", "bnb": "BNB", "xrp": "XRP", "ripple": "XRP", "ada": "ADA", "cardano": "ADA", "doge": "DOGE",
    "dogecoin": "DOGE", "avax": "AVAX", "avalanche": "AVAX", "link": "LINK", "chainlink": "LINK", "dot": "DOT",
    "polkadot": "DOT", "ltc": "LTC", "litecoin": "LTC", "matic": "POL", "polygon": "POL", "pol": "POL", "trx": "TRX",
    "tron": "TRX", "ton": "TON", "toncoin": "TON", "shib": "SHIB", "shiba": "SHIB", "pepe": "PEPE", "sui": "SUI",
    "apt": "APT", "aptos": "APT", "near": "NEAR", "atom": "ATOM", "cosmos": "ATOM", "uni": "UNI", "uniswap": "UNI",
    "xlm": "XLM", "stellar": "XLM", "bch": "BCH", "etc": "ETC", "fil": "FIL", "filecoin": "FIL", "arb": "ARB",
    "arbitrum": "ARB", "op": "OP", "optimism": "OP", "inj": "INJ", "injective": "INJ", "aave": "AAVE",
    "hbar": "HBAR", "hedera": "HBAR", "icp": "ICP", "render": "RENDER", "wif": "WIF", "bonk": "BONK", "sei": "SEI",
    "tia": "TIA", "celestia": "TIA", "ena": "ENA", "ondo": "ONDO", "jup": "JUP", "kas": "KAS", "algo": "ALGO",
    "algorand": "ALGO", "vet": "VET", "xmr": "XMR", "monero": "XMR", "trump": "TRUMP", "hype": "HYPE",
}
_STOCK_ALIASES = {
    "aapl": "NASDAQ:AAPL", "apple": "NASDAQ:AAPL", "tsla": "NASDAQ:TSLA", "tesla": "NASDAQ:TSLA",
    "nvda": "NASDAQ:NVDA", "nvidia": "NASDAQ:NVDA", "msft": "NASDAQ:MSFT", "microsoft": "NASDAQ:MSFT",
    "amzn": "NASDAQ:AMZN", "amazon": "NASDAQ:AMZN", "googl": "NASDAQ:GOOGL", "goog": "NASDAQ:GOOG",
    "google": "NASDAQ:GOOGL", "alphabet": "NASDAQ:GOOGL", "meta": "NASDAQ:META", "facebook": "NASDAQ:META",
    "nflx": "NASDAQ:NFLX", "netflix": "NASDAQ:NFLX", "amd": "NASDAQ:AMD", "intc": "NASDAQ:INTC", "intel": "NASDAQ:INTC",
    "avgo": "NASDAQ:AVGO", "broadcom": "NASDAQ:AVGO", "pltr": "NASDAQ:PLTR", "palantir": "NASDAQ:PLTR",
    "coin": "NASDAQ:COIN", "coinbase": "NASDAQ:COIN", "mstr": "NASDAQ:MSTR", "qqq": "NASDAQ:QQQ", "spy": "AMEX:SPY",
    "jpm": "NYSE:JPM", "ko": "NYSE:KO", "cocacola": "NYSE:KO", "dis": "NYSE:DIS", "disney": "NYSE:DIS",
    "brk": "NYSE:BRK.B", "berkshire": "NYSE:BRK.B", "v": "NYSE:V", "visa": "NYSE:V", "wmt": "NYSE:WMT",
    "walmart": "NYSE:WMT", "baba": "NYSE:BABA", "alibaba": "NYSE:BABA", "uber": "NYSE:UBER", "orcl": "NYSE:ORCL",
    "oracle": "NYSE:ORCL",
}
_MACRO_ALIASES = {
    "oro": "OANDA:XAUUSD", "gold": "OANDA:XAUUSD", "xau": "OANDA:XAUUSD", "xauusd": "OANDA:XAUUSD",
    # OANDA:XAGUSD and TVC:USOIL are not exposed by the TradingView scanner (verified): use listed equivalents.
    "plata": "TVC:SILVER", "silver": "TVC:SILVER", "xag": "TVC:SILVER", "xagusd": "TVC:SILVER",
    "petroleo": "NYMEX:CL1!", "oil": "NYMEX:CL1!", "wti": "NYMEX:CL1!", "crudo": "NYMEX:CL1!",
    "usoil": "NYMEX:CL1!", "brent": "ICEEUR:BRN1!", "gas": "NYMEX:NG1!", "gas natural": "NYMEX:NG1!",
    "natgas": "NYMEX:NG1!", "cobre": "COMEX:HG1!", "copper": "COMEX:HG1!",
    "spx": "SP:SPX", "sp500": "SP:SPX", "s&p500": "SP:SPX", "s&p 500": "SP:SPX", "s&p": "SP:SPX",
    "nasdaq": "NASDAQ:NDX", "ndx": "NASDAQ:NDX", "nas100": "NASDAQ:NDX", "nasdaq100": "NASDAQ:NDX",
    "dow": "DJ:DJI", "dji": "DJ:DJI", "dow jones": "DJ:DJI", "dowjones": "DJ:DJI", "us30": "DJ:DJI",
    "vix": "TVC:VIX", "dxy": "TVC:DXY", "dolar index": "TVC:DXY", "indice dolar": "TVC:DXY",
    "es": "CME_MINI:ES1!", "nq": "CME_MINI:NQ1!", "sim": "SIM:NEXUS", "nexus": "SIM:NEXUS",
    "sintetico": "SIM:NEXUS",
}
_FX_PAIRS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURJPY", "GBPJPY", "EURGBP",
             "EURCHF", "USDMXN", "USDBRL", "USDCOP", "USDCLP", "USDARS", "USDTRY", "USDZAR", "EURMXN")
_FX_ALIASES = {p.lower(): f"FX:{p}" for p in _FX_PAIRS}
_FX_ALIASES.update({
    "eur": "FX:EURUSD", "euro": "FX:EURUSD", "gbp": "FX:GBPUSD", "libra": "FX:GBPUSD", "jpy": "FX:USDJPY",
    "yen": "FX:USDJPY", "usddop": "FX_IDC:USDDOP", "dop": "FX_IDC:USDDOP", "peso dominicano": "FX_IDC:USDDOP",
    "mxn": "FX:USDMXN", "peso mexicano": "FX:USDMXN",
})


def normalize_text(text: str) -> str:
    """lowercase, accents removed, inner whitespace collapsed."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().split())


def alias_lookup(text: str, crypto_provider: str = "binance") -> str | None:
    """Static alias table used by resolve(): 'btc' -> 'BINANCE:BTCUSDT', 'oro' -> 'OANDA:XAUUSD'."""
    key = normalize_text(text)
    if not key:
        return None
    if key in _MACRO_ALIASES:
        return _MACRO_ALIASES[key]
    if key in _STOCK_ALIASES:
        return _STOCK_ALIASES[key]
    if key in _FX_ALIASES:
        return _FX_ALIASES[key]
    compact = key.replace(" ", "").replace("/", "").replace("-", "")
    if compact in _FX_ALIASES:
        return _FX_ALIASES[compact]
    base = _CRYPTO_ALIASES.get(key) or _CRYPTO_ALIASES.get(compact)
    if base:
        return crypto_symbol(crypto_provider, base, "USDT")
    # "solusdt", "eth/usdc", "btc-fdusd" -> stable-quoted crypto pair on the default exchange
    for quote in ("FDUSD", "USDT", "USDC"):
        up = compact.upper()
        if up.endswith(quote) and 2 <= len(up) - len(quote) <= 12 and up[: -len(quote)].isalnum():
            return crypto_symbol(crypto_provider, up[: -len(quote)], quote)
    return None


def alias_targets() -> set[str]:
    """Every symbol reachable through an alias (used to boost search results)."""
    out = set(_MACRO_ALIASES.values()) | set(_STOCK_ALIASES.values()) | set(_FX_ALIASES.values())
    out |= {f"BINANCE:{b}USDT" for b in _CRYPTO_ALIASES.values()}
    return out


# ----------------------------------------------------------------------------- parsing & validation


def clean_symbol(text: str) -> str:
    """Uppercase + validate 'EXCHANGE:TICKER'. Raises ValueError (Spanish) when malformed."""
    s = "".join(str(text or "").split()).upper()
    if not s:
        raise ValueError("Falta el símbolo (formato EXCHANGE:TICKER, por ejemplo BINANCE:BTCUSDT)")
    if not _SYMBOL_RE.match(s):
        raise ValueError(f"Símbolo no válido: {text!r} (formato EXCHANGE:TICKER, por ejemplo BINANCE:BTCUSDT)")
    return s


def is_sim(symbol: str) -> bool:
    return str(symbol).upper().startswith(SIM_EXCHANGE + ":")


def is_dex(symbol: str, description: str | None = None) -> bool:
    """DEX pools / on-chain junk that TradingView lists next to real markets."""
    exchange, ticker = parse_symbol(symbol)
    if any(m in exchange for m in _DEX_MARKERS):
        return True
    if exchange.endswith("DEX") and exchange not in ("INDEX", "NSEINDEX"):
        return True
    if _DEX_TICKER.search(ticker):
        return True
    return bool(description) and "(0x" in str(description).lower()


def normalize_base(base: str) -> str:
    return _BASE_ALIASES.get(base, base)


def strip_derivative(ticker: str) -> tuple[str, bool]:
    """'BTCUSDT.P' -> ('BTCUSDT', True); 'BTCUSD25Z2026' -> ('BTCUSD', True); 'BTCUSDT' -> ('BTCUSDT', False)."""
    t = ticker.upper()
    derivative = False
    if "." in t:
        t = t.split(".", 1)[0]
        derivative = True
    m = _EXPIRY.search(t)
    if m and len(t) - len(m.group(1)) >= 5:
        t = t[: m.start()]
        derivative = True
    return t, derivative


def split_pair(ticker: str, currency: str | None = None) -> tuple[str, str] | None:
    """'BTCUSDT' -> ('BTC', 'USDT'); uses the TradingView `currency` hint first. None when it cannot split."""
    t = ticker.upper().replace("-", "").replace("/", "").replace("_", "")
    cur = (currency or "").upper()
    if cur and t.endswith(cur) and len(t) > len(cur) + 1:
        return normalize_base(t[: -len(cur)]), normalize_base(cur)
    for quote in KNOWN_QUOTES:
        if t.endswith(quote) and len(t) - len(quote) >= 2:
            base = t[: -len(quote)]
            if base.isalnum():
                return normalize_base(base), quote
    if t.endswith("XBT") and len(t) > 4:  # Kraken style ETHXBT
        return normalize_base(t[:-3]), "BTC"
    return None


def fx_pair(ticker: str) -> tuple[str, str] | None:
    t = ticker.upper()
    if len(t) == 6 and t.isalpha():
        a, b = t[:3], t[3:]
        if (a in FIAT_CODES or a in METAL_CODES) and b in FIAT_CODES:
            return a, b
    return None


# ----------------------------------------------------------------------------- crypto venues & routing


@dataclass(frozen=True)
class Venue:
    """One official exchange instrument able to serve candles for a symbol."""

    provider: str  # binance | okx | coinbase | kraken | kucoin
    base: str
    quote: str

    @property
    def exchange(self) -> str:
        return PROVIDER_EXCHANGE[self.provider]

    @property
    def tv_symbol(self) -> str:
        return f"{self.exchange}:{self.base}{self.quote}"

    @property
    def native(self) -> str:
        """Instrument id expected by the exchange REST API (argument `native` of providers.fetch_candles)."""
        return native_symbol(self.provider, self.base, self.quote)


@dataclass(frozen=True)
class Route:
    symbol: str
    kind: str  # "sim" | "crypto" | "other"
    asset_class: str
    venues: tuple[Venue, ...] = field(default_factory=tuple)
    native: bool = False  # venues[0] is the very instrument of `symbol` (otherwise an equivalent pair)


# Kraken still names bitcoin XBT in its REST API (DOGE works as-is; verified with live samples).
_KRAKEN_CODES = {"BTC": "XBT"}


def native_symbol(provider: str, base: str, quote: str) -> str:
    """'binance', 'BTC', 'USDT' -> 'BTCUSDT'; okx/kucoin/coinbase -> 'BTC-USDT'; kraken -> 'XBTUSDT'."""
    b, q = str(base).upper(), str(quote).upper()
    if provider in ("okx", "kucoin", "coinbase"):
        return f"{b}-{q}"
    if provider == "kraken":
        return f"{_KRAKEN_CODES.get(b, b)}{_KRAKEN_CODES.get(q, q)}"
    return f"{b}{q}"


def route_note(route: Route, venue: Venue) -> str | None:
    """Spanish note shown when the candles come from an equivalent pair instead of the symbol itself."""
    if route.native and route.venues and venue == route.venues[0]:
        return None
    return f"Datos de {venue.tv_symbol} (par equivalente)"


def crypto_symbol(provider: str, base: str, quote: str) -> str:
    return f"{PROVIDER_EXCHANGE.get(provider, provider.upper())}:{base}{quote}"


def _equivalent_quote(provider: str, quote: str) -> str:
    if provider in ("binance", "okx", "kucoin") and quote in ("USD", "BUSD"):
        return "USDT"
    return quote


def _equivalent_order(preferred: str) -> list[str]:
    order = ["binance", "okx", "kucoin"]
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    elif preferred in ("coinbase", "kraken"):
        order.insert(0, preferred)
    return order


def crypto_venues(provider: str | None, base: str, quote: str, preferred: str = "binance") -> list[Venue]:
    """Native venue first (when `provider` is a direct exchange), then equivalent pairs, without duplicates."""
    out: list[Venue] = []
    if provider:
        out.append(Venue(provider, base, quote))
    for p in _equivalent_order(preferred):
        q = _equivalent_quote(p, quote) if p in ("binance", "okx", "kucoin") else quote
        if p in ("coinbase", "kraken") and q in ("USDT", "BUSD", "FDUSD"):
            q = "USD" if p == "coinbase" else "USDT"
        v = Venue(p, base, q)
        if v not in out:
            out.append(v)
    return out


def asset_class_of(exchange: str, ticker: str, tv_type: str | None = None, market: str | None = None) -> str:
    """crypto | stock | fund | forex | commodity | index | futures | cfd | bond | other."""
    t = (tv_type or "").strip().lower()
    m = (market or "").strip().lower()
    if t in ("spot", "swap", "crypto") or m in ("crypto", "coin"):
        return "crypto"
    mapping = {
        "forex": "forex", "index": "index", "futures": "futures", "commodity": "commodity", "cfd": "cfd",
        "bond": "bond", "fund": "fund", "etf": "fund", "stock": "stock", "dr": "stock", "structured": "stock",
        "preferred": "stock", "warrant": "stock", "economic": "other",
    }
    if t in mapping:
        return mapping[t]
    ex = exchange.upper()
    tk = ticker.upper()
    if ex in DIRECT_CEX or ex in CRYPTO_EXCHANGES:
        return "crypto"
    if ex in FOREX_EXCHANGES:
        pair = fx_pair(tk)
        if pair:
            return "commodity" if pair[0] in METAL_CODES else "forex"
        return "cfd"
    if ex in FUTURES_EXCHANGES or tk.endswith("!"):
        return "futures"
    if ex in ("SP", "DJ", "INDEX", "CRYPTOCAP", "NSEINDEX") or (ex == "TVC" and tk in ("DXY", "VIX", "US10Y")):
        return "index"
    if ex == "TVC":
        return "cfd"
    if ex in ("FINRA", "TVCBOND"):
        return "bond"
    if is_dex(f"{ex}:{tk}"):
        return "crypto"  # on-chain pool (route_symbol still refuses to map it to an exchange pair)
    return "stock"


def market_for(exchange: str, ticker: str = "", tv_type: str | None = None) -> str:
    """Best scanner market for a symbol (falls back to 'global')."""
    ex = exchange.upper()
    if ex in DIRECT_CEX or ex in CRYPTO_EXCHANGES or (tv_type or "").lower() in ("spot", "swap"):
        return "crypto"
    if ex in _EXCHANGE_MARKET:
        return _EXCHANGE_MARKET[ex]
    if ex in FUTURES_EXCHANGES or ticker.endswith("!"):
        return "futures"
    if ex in FOREX_EXCHANGES:
        pair = fx_pair(ticker)
        if pair and pair[0] not in METAL_CODES:
            return "forex"
        return "cfd"
    if ex == "FINRA":
        return "bond"
    return "global"


def route_symbol(symbol: str, info: dict | None = None, preferred: str = "binance") -> Route:
    """Decide where the candles of `symbol` come from (no I/O)."""
    exchange, ticker = parse_symbol(symbol)
    info = info or {}
    if exchange == SIM_EXCHANGE:
        return Route(symbol, "sim", "synthetic")
    tv_type = str(info.get("type") or "")
    market = str(info.get("market") or "")
    currency = str(info.get("currency") or "")
    aclass = asset_class_of(exchange, ticker, tv_type, market)
    crypto_like = exchange in DIRECT_CEX or exchange in CRYPTO_EXCHANGES or (
        aclass == "crypto" and not is_dex(symbol, info.get("description")) and exchange not in ("CRYPTOCAP", "INDEX")
    )
    if crypto_like and not is_dex(symbol, info.get("description")):
        plain, derivative = strip_derivative(ticker)
        pair = split_pair(plain, None if derivative else currency)
        if pair is not None:
            base, quote = pair
            direct = DIRECT_CEX.get(exchange)
            if direct and not derivative:
                venues = crypto_venues(direct, base, quote, preferred)
                return Route(symbol, "crypto", "crypto", tuple(venues), native=True)
            return Route(symbol, "crypto", "crypto", tuple(crypto_venues(None, base, quote, preferred)), native=False)
    return Route(symbol, "other", aclass if aclass != "crypto" else "other")


# ----------------------------------------------------------------------------- keyed providers mapping

_TD_EXCHANGES = {
    "LSE": "LSE", "XETR": "XETR", "TSX": "TSX", "TSXV": "TSXV", "ASX": "ASX", "NSE": "NSE", "BSE": "BSE",
    "HKEX": "HKEX", "BMV": "BMV", "BME": "BME", "SIX": "SIX", "EURONEXT": "Euronext", "BMFBOVESPA": "Bovespa",
}
_TD_INDEX = {"SPX": "SPX", "NDX": "NDX", "DJI": "DJI", "IXIC": "IXIC", "VIX": "VIX", "RUT": "RUT"}
_AV_SUFFIX = {"LSE": "LON", "TSX": "TRT", "TSXV": "TRV", "XETR": "DEX", "BSE": "BSE", "SSE": "SHH", "SZSE": "SHZ"}


def twelvedata_params(symbol: str, asset_class: str) -> dict | None:
    """Query parameters identifying `symbol` in Twelve Data, or None when not supported."""
    exchange, ticker = parse_symbol(symbol)
    pair = fx_pair(ticker)
    if pair and asset_class in ("forex", "commodity", "cfd"):
        return {"symbol": f"{pair[0]}/{pair[1]}"}
    if asset_class == "index":
        return {"symbol": _TD_INDEX.get(ticker, ticker)}
    if asset_class in ("stock", "fund"):
        params = {"symbol": ticker}
        if exchange in _TD_EXCHANGES:
            params["exchange"] = _TD_EXCHANGES[exchange]
        elif exchange not in US_STOCK_EXCHANGES:
            return None
        return params
    if asset_class == "commodity" and ticker in ("GOLD", "SILVER"):
        return {"symbol": "XAU/USD" if ticker == "GOLD" else "XAG/USD"}
    return None


def polygon_ticker(symbol: str, asset_class: str) -> str | None:
    exchange, ticker = parse_symbol(symbol)
    pair = fx_pair(ticker)
    if pair and asset_class in ("forex", "commodity", "cfd"):
        return f"C:{pair[0]}{pair[1]}"
    if asset_class == "index" and exchange in ("SP", "DJ", "NASDAQ", "CBOE", "TVC"):
        return f"I:{ticker}"
    if asset_class in ("stock", "fund") and exchange in US_STOCK_EXCHANGES:
        return ticker
    return None


def alphavantage_spec(symbol: str, asset_class: str) -> dict | None:
    exchange, ticker = parse_symbol(symbol)
    pair = fx_pair(ticker)
    if pair and asset_class == "forex" and pair[0] not in METAL_CODES:
        return {"kind": "fx", "from": pair[0], "to": pair[1]}
    if asset_class in ("stock", "fund"):
        if exchange in US_STOCK_EXCHANGES:
            return {"kind": "stock", "symbol": ticker}
        if exchange in _AV_SUFFIX:
            return {"kind": "stock", "symbol": f"{ticker}.{_AV_SUFFIX[exchange]}"}
    return None


# ----------------------------------------------------------------------------- search ranking

_TYPE_RANK = {
    "spot": 0, "stock": 0, "forex": 0, "index": 0, "crypto": 0, "commodity": 0, "fund": 1, "cfd": 1, "dr": 2,
    "futures": 2, "swap": 3, "bond": 4, "structured": 4, "warrant": 4, "option": 5, "economic": 5,
}
LIQUIDITY_CAP = 11.0  # log10 of traded value; caps absurd DEX / contract-unit volumes


def liquidity_score(row: dict) -> float:
    """Capped log10 of the traded value (USD-ish) of a TradingView row."""
    value = None
    for key in ("24h_vol|5", "Value.Traded"):
        v = row.get(key)
        if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
            value = float(v)
            break
    if value is None:
        c, vol = row.get("close"), row.get("volume")
        if isinstance(c, (int, float)) and isinstance(vol, (int, float)) and c > 0 and vol > 0:
            value = float(c) * float(vol)
    if value is None or not math.isfinite(value) or value <= 0:
        return 0.0
    return min(math.log10(1.0 + value), LIQUIDITY_CAP)


def match_tier(query: str, ticker: str, description: str) -> int | None:
    """0 exact ticker · 1 crypto base · 2 ticker prefix · 3 description prefix/word · 4 ticker contains ·
    5 description contains · None = no match."""
    q = re.sub(r"[^A-Z0-9]", "", query.upper())
    if not q:
        return None
    t = ticker.upper()
    t_plain = t.split(".", 1)[0]
    if t == q or t_plain == q:
        return 0
    pair = split_pair(t_plain)
    if pair is not None and pair[0] == normalize_base(q):
        return 1
    if t.startswith(q):
        return 2
    d = normalize_text(description)
    qt = normalize_text(query)
    if qt and (d.startswith(qt) or f" {qt}" in f" {d}"):
        return 3
    if q in t:
        return 4
    if qt and qt in d:
        return 5
    return None


# match tier -> relevance group: an exact ticker and a crypto base match ("btc" -> BTCUSDT) are equally relevant
_TIER_GROUP = {0: 0, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4}


def rank_results(query: str, items: Iterable[dict], boost: set[str] | None = None) -> list[dict]:
    """Drop DEX junk / non-matches, then sort: relevance group > known/alias > type > preferred venue > exact
    ticker before base match > liquidity (capped) > shorter ticker."""
    boost = boost or set()
    scored: list[tuple[tuple, dict]] = []
    seen: set[str] = set()
    for item in items:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol or symbol in seen:
            continue
        description = str(item.get("description") or "")
        if is_dex(symbol, description):
            continue
        exchange, ticker = parse_symbol(symbol)
        tier = match_tier(query, str(item.get("ticker") or ticker), description)
        if tier is None:
            continue
        seen.add(symbol)
        known = 0 if (symbol in boost or item.get("_known")) else 1
        type_rank = _TYPE_RANK.get(str(item.get("type") or "").lower(), 3)
        venue = _VENUE_RANK.get(exchange, len(PREFERRED_VENUES))
        liq = float(item.get("_liquidity") or 0.0)
        key = (_TIER_GROUP[tier], known, type_rank, venue, tier, -round(liq, 1), len(ticker), symbol)
        scored.append((key, item))
    scored.sort(key=lambda kv: kv[0])
    return [item for _, item in scored]
