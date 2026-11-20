"""Default settings of the practice-broker mirror (app.broker): NEXUS automática repeated in an Alpaca PAPER account.
Off until the user turns it on with the keys of a practice account (secrets: their names contain "key")."""
from __future__ import annotations

DEFAULTS: dict = {
    "broker.alpaca_enabled": False,
    "broker.alpaca_key_id": "",  # API Key ID of the PAPER account: a secret, masked in the terminal, never logged
    "broker.alpaca_secret_key": "",  # its Secret Key: a secret
    "broker.mirror_stocks": True,  # US stocks and ETFs
    "broker.mirror_crypto": True,  # crypto quoted in dollars (Alpaca has no crypto shorts)
    # NEXUS automática repeated on Binance's TESTNETS (app.broker.binance_mirror: test funds, never money). The spot
    # and futures testnets issue separate keys (secrets: their names contain "key" / "secret").
    "broker.binance_enabled": False,
    "broker.binance_spot_key": "",
    "broker.binance_spot_secret": "",
    "broker.binance_futures_key": "",
    "broker.binance_futures_secret": "",
    "broker.binance_mirror_spot": True,  # spot crypto in dollars, long only (a wallet: buys, later sells)
    "broker.binance_mirror_perps": True,  # crypto perpetuals (.P), long and short, with autopilot.perp_leverage
    # NEXUS automática repeated in a MetaTrader 5 account of any broker (app.broker.mt5_mirror): the account logged in
    # in the MT5 terminal of this Windows machine (no password is stored). Demo accounts right away; a REAL account
    # only when the user switches mt5_allow_real on AND types that account's number in mt5_real_login.
    "broker.mt5_enabled": False,
    "broker.mt5_path": "",  # terminal64.exe of the MT5 to use (empty: the one already open / the default install)
    "broker.mt5_symbols": "",  # the broker's names when not found by itself: "XAU=GOLD, BTC=BTCUSD.m"
    "broker.mt5_max_lots": 0.1,  # cap per order, every account type (0 = send nothing)
    "broker.mt5_allow_real": False,  # the user's own switch: never turned on by the app
    "broker.mt5_real_login": "",  # the only REAL account number allowed while mt5_allow_real is on
    "broker.mt5_account": "1k",  # which NEXUS account it copies: "1k" (the 1k account) or "10k" (account 3)
}
