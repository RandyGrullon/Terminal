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
}
