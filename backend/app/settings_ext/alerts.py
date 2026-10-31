"""Default settings of the alerts feature (merged into DEFAULT_SETTINGS).

Alerts outside the app (app.alerts): what is forwarded (categories, minimum priority, symbols, hourly limit, quiet
hours) and the user's own channels. Every channel is opt-in and off by default; nothing is sent until the user
enables a channel and completes its data. Secrets (bot token, SMTP password) are settings of kind secret in the UI.
"""
from __future__ import annotations

DEFAULTS: dict = {
    # what is forwarded
    "alerts.macro": True,  # semáforo: level changes and T−60 / T−15 / T−0 of high-impact events
    "alerts.signals": True,  # NEXUS entries, exits and early exits of the watched symbols (watchlist at watch.tf)
    "alerts.paper": True,  # demo account (account 1): opened, closed, stops (simulated)
    "alerts.min_level": "warn",  # info (todos) | warn (importantes y críticos) | critical (solo críticos)
    # only these watched symbols get signal alerts (empty = the whole watchlist; 'AAPL' = that ticker on any exchange)
    "alerts.signal_symbols": [],
    "alerts.max_per_hour": 20,  # messages per rolling hour (critical ones may exceed it, up to max(2 × limit, 60))
    # quiet hours, in the user's zone (ui.timezone, else the server's)
    "alerts.quiet_enabled": False,
    "alerts.quiet_start": "23:00",
    "alerts.quiet_end": "07:00",
    "alerts.quiet_critical": False,  # critical alerts still go out during the quiet hours
    # Telegram (the user's own bot: https://api.telegram.org, sendMessage)
    "alerts.telegram_enabled": False,
    "alerts.telegram_token": "",
    "alerts.telegram_chat_id": "",
    # email (SMTP)
    "alerts.email_enabled": False,
    "alerts.smtp_host": "",
    "alerts.smtp_port": 587,
    "alerts.smtp_security": "starttls",  # starttls | ssl | none
    "alerts.smtp_user": "",
    "alerts.smtp_password": "",
    "alerts.email_from": "",  # empty = the SMTP user (when it is an address)
    "alerts.email_to": "",  # one or more addresses separated by commas
}
