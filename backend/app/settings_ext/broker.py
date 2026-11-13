"""Default settings of the practice-broker mirror (app.broker): NEXUS automática repeated in an Alpaca PAPER account.
Off until the user turns it on with the keys of a practice account (secrets: their names contain "key")."""
from __future__ import annotations

DEFAULTS: dict = {
    "broker.alpaca_enabled": False,
    "broker.alpaca_key_id": "",  # API Key ID of the PAPER account: a secret, masked in the terminal, never logged
    "broker.alpaca_secret_key": "",  # its Secret Key: a secret
    "broker.mirror_stocks": True,  # US stocks and ETFs
    "broker.mirror_crypto": True,  # crypto quoted in dollars (Alpaca has no crypto shorts)
}
