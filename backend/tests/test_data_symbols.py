"""Symbol knowledge: routing to exchange APIs, aliases, DEX filtering, search ranking, keyed mappings (offline)."""
from __future__ import annotations

import pytest

from app.data import symbols as S


# ----------------------------------------------------------------------------- routing


def test_route_native_binance_pair_with_equivalents():
    r = S.route_symbol("BINANCE:BTCUSDT")
    assert r.kind == "crypto" and r.asset_class == "crypto" and r.native is True
    assert r.venues[0] == S.Venue("binance", "BTC", "USDT")
    assert [v.provider for v in r.venues] == ["binance", "okx", "kucoin"]
    assert r.venues[0].native == "BTCUSDT" and r.venues[1].native == "BTC-USDT"
    assert S.route_note(r, r.venues[0]) is None
    assert S.route_note(r, r.venues[1]) == "Datos de OKX:BTCUSDT (par equivalente)"


@pytest.mark.parametrize(
    "symbol, first",
    [
        ("BINANCE:BTCUSDT.P", "BINANCE:BTCUSDT"),  # perpetual -> spot pair
        ("COINBASE:BTCUSD.P", "BINANCE:BTCUSDT"),  # USD -> USDT on Binance
        ("BYBIT:ETHUSDT", "BINANCE:ETHUSDT"),  # Bybit answers 403 here: equivalent pair
        ("OKX:BTCUSD25Z2026", "BINANCE:BTCUSDT"),  # dated future
        ("CRYPTO:BTCUSD", "BINANCE:BTCUSDT"),
        ("BITSTAMP:XRPUSD", "BINANCE:XRPUSDT"),
    ],
)
def test_route_equivalent_pairs(symbol, first):
    r = S.route_symbol(symbol)
    assert r.kind == "crypto" and r.native is False
    assert r.venues[0].tv_symbol == first
    assert S.route_note(r, r.venues[0]) == f"Datos de {first} (par equivalente)"


def test_route_kraken_and_coinbase_native_ids():
    r = S.route_symbol("KRAKEN:XBTUSD")
    assert r.native and r.venues[0] == S.Venue("kraken", "BTC", "USD")
    assert r.venues[0].native == "XBTUSD"  # Kraken still calls bitcoin XBT
    assert S.route_symbol("COINBASE:ETHUSD").venues[0].native == "ETH-USD"
    assert S.route_symbol("KUCOIN:SOLUSDT").venues[0].native == "SOL-USDT"
    assert S.native_symbol("kraken", "ETH", "BTC") == "ETHXBT"
    assert S.native_symbol("binance", "eth", "btc") == "ETHBTC"


def test_route_respects_preferred_exchange():
    r = S.route_symbol("BYBIT:SOLUSDT", preferred="okx")
    assert [v.provider for v in r.venues][:3] == ["okx", "binance", "kucoin"]
    r = S.route_symbol("BYBIT:SOLUSDT", preferred="coinbase")
    assert r.venues[0] == S.Venue("coinbase", "SOL", "USD")


@pytest.mark.parametrize(
    "symbol, kind, aclass",
    [
        ("NASDAQ:AAPL", "other", "stock"),
        ("FX:EURUSD", "other", "forex"),
        ("OANDA:XAUUSD", "other", "commodity"),
        ("CRYPTOCAP:BTC", "other", "index"),
        ("SP:SPX", "other", "index"),
        ("NYMEX:CL1!", "other", "futures"),
        ("SIM:NEXUS", "sim", "synthetic"),
        ("UNISWAP3ETH:WBTCWETH_4585FE", "other", "other"),
    ],
)
def test_route_non_exchange_symbols(symbol, kind, aclass):
    r = S.route_symbol(symbol)
    assert (r.kind, r.asset_class) == (kind, aclass)
    assert r.venues == ()


def test_route_uses_tradingview_info():
    r = S.route_symbol("BINANCE:ETHBTC", {"type": "spot", "currency": "BTC"})
    assert r.venues[0] == S.Venue("binance", "ETH", "BTC")
    # a crypto-typed asset of an unknown CEX still gets an equivalent pair
    r = S.route_symbol("NEWCEX:ADAUSDT", {"type": "spot", "market": "crypto", "currency": "USDT"})
    assert r.kind == "crypto" and r.venues[0].tv_symbol == "BINANCE:ADAUSDT"


def test_pair_helpers():
    assert S.strip_derivative("BTCUSDT.P") == ("BTCUSDT", True)
    assert S.strip_derivative("BTCUSD25Z2026") == ("BTCUSD", True)
    assert S.strip_derivative("BTCUSDT") == ("BTCUSDT", False)
    assert S.split_pair("BTCUSDT") == ("BTC", "USDT")
    assert S.split_pair("ARBUSD") == ("ARB", "USD")
    assert S.split_pair("XBTUSD") == ("BTC", "USD")
    # bases ending in T must not be read as TUSD pairs (DOT/USD, GRT/USD, FET/USD)
    assert [S.split_pair(t) for t in ("DOTUSD", "GRTUSD", "FETUSD")] == [("DOT", "USD"), ("GRT", "USD"), ("FET", "USD")]
    assert S.split_pair("BTCTUSD", currency="TUSD") == ("BTC", "TUSD")  # TradingView's currency hint
    assert S.split_pair("ETHXBT") == ("ETH", "BTC")
    assert S.split_pair("AAPL") is None
    assert S.fx_pair("EURUSD") == ("EUR", "USD") and S.fx_pair("BTCUSD") is None


# ----------------------------------------------------------------------------- aliases & validation


@pytest.mark.parametrize(
    "text, expected",
    [
        ("btc", "BINANCE:BTCUSDT"), ("Bitcoin", "BINANCE:BTCUSDT"), ("  ETH ", "BINANCE:ETHUSDT"),
        ("oro", "OANDA:XAUUSD"), ("Petróleo", "NYMEX:CL1!"), ("aapl", "NASDAQ:AAPL"), ("eurusd", "FX:EURUSD"),
        ("eur/usd", "FX:EURUSD"), ("solusdt", "BINANCE:SOLUSDT"), ("pepe-fdusd", "BINANCE:PEPEFDUSD"),
        ("s&p 500", "SP:SPX"), ("peso dominicano", "FX_IDC:USDDOP"), ("sim", "SIM:NEXUS"),
    ],
)
def test_alias_lookup(text, expected):
    assert S.alias_lookup(text) == expected


def test_alias_lookup_other_exchange_and_misses():
    assert S.alias_lookup("btc", crypto_provider="okx") == "OKX:BTCUSDT"
    assert S.alias_lookup("zzqqxx") is None
    assert S.alias_lookup("") is None
    assert "BINANCE:BTCUSDT" in S.alias_targets() and "NASDAQ:AAPL" in S.alias_targets()


def test_clean_symbol_and_markets():
    assert S.clean_symbol(" binance:btcusdt ") == "BINANCE:BTCUSDT"
    assert S.clean_symbol("NYSE:BRK.B") == "NYSE:BRK.B"
    with pytest.raises(ValueError, match="Símbolo no válido"):
        S.clean_symbol("BTCUSDT")
    with pytest.raises(ValueError, match="Falta el símbolo"):
        S.clean_symbol("   ")
    assert S.valid_market("crypto") and S.valid_market("america")
    assert not S.valid_market("../etc") and not S.valid_market("") and not S.valid_market(None)
    assert S.MARKET_NAMES["america"] == "EE. UU." and S.MARKET_NAMES["crypto"] == "Cripto"
    assert S.is_sim("sim:nexus") and not S.is_sim("BINANCE:SIMUSDT")


@pytest.mark.parametrize(
    "exchange, ticker, tv_type, market",
    [
        ("BINANCE", "BTCUSDT", None, "crypto"), ("NASDAQ", "AAPL", None, "america"), ("FX", "EURUSD", None, "forex"),
        ("OANDA", "XAUUSD", None, "cfd"), ("TVC", "DXY", None, "cfd"), ("NYMEX", "CL1!", None, "futures"),
        ("BMV", "AMXL", None, "mexico"), ("FINRA", "AAPL6070268", None, "bond"), ("XYZ", "ABC", "swap", "crypto"),
        ("UNKNOWNEX", "ABC", None, "global"),
    ],
)
def test_market_for(exchange, ticker, tv_type, market):
    assert S.market_for(exchange, ticker, tv_type) == market


# ----------------------------------------------------------------------------- DEX filter & ranking


@pytest.mark.parametrize(
    "symbol, description",
    [
        ("UNISWAP3POLYGON:APPLEUSDT_8F6463", "Apple / USDT0 on Polygon (0x8F6463FBd1e5992e14c438D61ebeEc4b77B1dabd)"),
        ("PANCAKESWAP:CZAAPLB_8BA5A4", "CZ / Apple on BSC"),
        ("RAYDIUMCLMM:WBTCUSDC_8MEJ5V.USD", "Wrapped BTC / USD Coin on Solana in USD"),
        ("PULSEX:WBTCATRO_E77506.USD", ""),
        ("SOMEDEX:ABCUSD", ""),
        ("NEWCHAIN:TOKEN", "Token on Base (0xabc)"),
    ],
)
def test_is_dex_true(symbol, description):
    assert S.is_dex(symbol, description)


@pytest.mark.parametrize("symbol", ["BINANCE:BTCUSDT", "NSEINDEX:NIFTY", "FX_IDC:USDDOP", "NYSE:BRK.B",
                                    "OKX:BTCUSDT.P", "INDEX:BTCUSD", "BITMEX:XBTUSD"])
def test_is_dex_false(symbol):
    assert not S.is_dex(symbol, "Bitcoin")


# trimmed real answer of the scanner for name/description ~ "apple" sorted by volume (DEX pools first)
APPLE_ROWS = [
    {"symbol": "UNISWAP3POLYGON:APPLEUSDT_8F6463", "description": "Apple / USDT0 on Polygon (0x8F6463FBd1e5)",
     "type": "spot", "_liquidity": 11.0},
    {"symbol": "PANCAKESWAP:CZAAPLB_8BA5A4", "description": "CZ / Apple on BSC (0x8bA5a4410FDF)", "type": "spot"},
    {"symbol": "FINRA:AAPL6070268", "description": "Apple Inc. 4.0% 12-MAY-2028", "type": "bond", "_liquidity": 7.1},
    {"symbol": "SSE:603020", "description": "Apple Flavor & Fragrance Group Co. Ltd. Class A", "type": "stock",
     "_liquidity": 7.0},
    {"symbol": "NASDAQ:AAPL", "description": "Apple Inc.", "type": "stock", "_liquidity": 9.9},
]


def test_rank_results_apple():
    ranked = S.rank_results("apple", APPLE_ROWS, boost=S.alias_targets())
    symbols = [r["symbol"] for r in ranked]
    assert symbols[0] == "NASDAQ:AAPL"
    assert not any("SWAP" in s for s in symbols)
    assert symbols.index("FINRA:AAPL6070268") > symbols.index("SSE:603020")  # bonds after stocks


def test_rank_results_ticker_tiers_and_dedupe():
    items = [
        {"symbol": "LSE:BTC", "description": "London BTC Company Limited", "type": "stock", "_liquidity": 5.4},
        {"symbol": "OKX:BTCUSDT.P", "description": "BTCUSDT Perpetual Swap Contract", "type": "swap",
         "_liquidity": 11.0},
        {"symbol": "COINBASE:BTCUSD", "description": "Bitcoin / U.S. Dollar", "type": "spot", "_liquidity": 9.5},
        {"symbol": "BINANCE:BTCUSDT", "description": "Bitcoin / TetherUS", "type": "spot", "_liquidity": 9.2},
        {"symbol": "BINANCE:BTCUSDT", "description": "duplicate", "type": "spot"},
        {"symbol": "NASDAQ:MSFT", "description": "Microsoft", "type": "stock"},
    ]
    ranked = [r["symbol"] for r in S.rank_results("btc", items, boost=S.alias_targets())]
    assert ranked[0] == "BINANCE:BTCUSDT"  # alias target first
    assert ranked.index("COINBASE:BTCUSD") < ranked.index("OKX:BTCUSDT.P")  # spot before perpetual
    assert "NASDAQ:MSFT" not in ranked and ranked.count("BINANCE:BTCUSDT") == 1
    assert S.match_tier("aapl", "AAPL", "Apple Inc.") == 0
    assert S.match_tier("app", "AAPL", "Apple Inc.") == 3
    assert S.match_tier("zzz", "AAPL", "Apple Inc.") is None


def test_liquidity_score_is_capped():
    assert S.liquidity_score({"24h_vol|5": 228303390255950.0}) == S.LIQUIDITY_CAP
    assert 6.9 < S.liquidity_score({"close": 100.0, "volume": 100_000}) < 7.1
    assert S.liquidity_score({"close": None, "volume": None}) == 0.0


# ----------------------------------------------------------------------------- keyed mappings


def test_keyed_symbol_mappings():
    assert S.twelvedata_params("NASDAQ:AAPL", "stock") == {"symbol": "AAPL"}
    assert S.twelvedata_params("LSE:VOD", "stock") == {"symbol": "VOD", "exchange": "LSE"}
    assert S.twelvedata_params("FX:EURUSD", "forex") == {"symbol": "EUR/USD"}
    assert S.twelvedata_params("BINANCE:BTCUSDT", "crypto") is None
    assert S.polygon_ticker("FX:EURUSD", "forex") == "C:EURUSD"
    assert S.polygon_ticker("NYSE:KO", "stock") == "KO"
    assert S.polygon_ticker("SP:SPX", "index") == "I:SPX"
    assert S.alphavantage_spec("NYSE:IBM", "stock") == {"kind": "stock", "symbol": "IBM"}
    assert S.alphavantage_spec("LSE:VOD", "stock") == {"kind": "stock", "symbol": "VOD.LON"}
    assert S.alphavantage_spec("FX:EURUSD", "forex") == {"kind": "fx", "from": "EUR", "to": "USD"}
    assert S.alphavantage_spec("OANDA:XAUUSD", "commodity") is None
