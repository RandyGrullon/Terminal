"""Integration round 2 (core): the terminal never shows a credential in clear and never writes one to the event log.

- `settings` (listing, single read, change; aliases; `clave=valor` as one word) masks every credential setting: the
  provider keys, the Telegram bot token and the SMTP password, not only keys containing "key";
- the command line persisted as a debug event (`> settings alerts.smtp_password ••••••`) and the error event of a
  failing command never carry the value; other command lines are logged as typed.
"""
from __future__ import annotations

import asyncio

from app.config import DEFAULT_SETTINGS
from app.terminal import SECRET_MASK, execute, is_secret_setting, loggable_command

SECRET = "s3cr3to-de-prueba-9F2"
CREDENTIALS = ("providers.twelvedata_key", "providers.alphavantage_key", "providers.polygon_key",
               "alerts.telegram_token", "alerts.smtp_password")


def _text(result: dict) -> str:
    return "\n".join(line["text"] for line in result["lines"])


def _logged(rt) -> list[str]:
    return [e["message"] for e in rt.bus.recent(200, source="terminal")]


def test_every_credential_setting_is_recognised_and_plain_settings_are_not():
    for key in CREDENTIALS:
        assert key in DEFAULT_SETTINGS and is_secret_setting(key), key
    for key in ("alerts.smtp_user", "alerts.smtp_host", "alerts.telegram_chat_id", "alerts.email_to",
                "alerts.email_from", "nexus.horizon", "scanner.replay_days", "ui.timezone", "realtime.max_symbols"):
        assert not is_secret_setting(key), key
    # whatever the predicate catches among the defaults is text (a credential), never a number or a switch
    for key in (k for k in DEFAULT_SETTINGS if is_secret_setting(k)):
        assert isinstance(DEFAULT_SETTINGS[key], str), key


def test_loggable_command_hides_only_credential_values():
    assert loggable_command("settings alerts.smtp_password abc def", "settings", "settings",
                            ["alerts.smtp_password", "abc", "def"]) == f"settings alerts.smtp_password {SECRET_MASK}"
    assert loggable_command("config providers.polygon_key=XYZ", "config", "settings",
                            ["providers.polygon_key=XYZ"]) == f"config providers.polygon_key {SECRET_MASK}"
    # a slip that names a credential elsewhere in the line: logged without its arguments
    assert loggable_command("config set alerts.smtp_password abc", "config", "settings",
                            ["set", "alerts.smtp_password", "abc"]) == f"config {SECRET_MASK}"
    assert loggable_command("settings --x providers.polygon_key=XYZ", "settings", "settings", []) \
        == f"settings {SECRET_MASK}"
    # a plain read of a credential has nothing to hide; other settings and other commands are logged as typed
    assert loggable_command("settings alerts.telegram_token", "settings", "settings",
                            ["alerts.telegram_token"]) == "settings alerts.telegram_token"
    assert loggable_command("settings nexus.horizon 7", "settings", "settings", ["nexus.horizon", "7"]) \
        == "settings nexus.horizon 7"
    assert loggable_command("settings alerts.email_to token@example.com", "settings", "settings",
                            ["alerts.email_to", "token@example.com"]) == "settings alerts.email_to token@example.com"
    assert loggable_command("quote btc", "quote", "quote", ["btc"]) == "quote btc"


def test_settings_listing_reads_and_changes_never_print_a_credential(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            unset = "providers.alphavantage_key"
            for key in CREDENTIALS:
                if key != unset:
                    await rt.settings.set(key, f"{SECRET}-{key}")
            await rt.settings.set("alerts.smtp_user", "yo@example.com")

            listing = await execute("settings")
            assert listing["ok"] and SECRET not in _text(listing)
            rows = {line["text"].split(None, 1)[0]: line["text"].split(None, 1)[1:] for line in listing["lines"]}
            for key in CREDENTIALS:
                assert rows[key] == ([] if key == unset else [SECRET_MASK]), key  # an empty one shows as empty
            assert rows["alerts.smtp_user"] == ["yo@example.com"]  # not a credential: shown

            read = await execute("settings alerts.smtp_password")
            assert _text(read) == f"alerts.smtp_password = {SECRET_MASK}"

            changed = await execute(f'ajustes alerts.smtp_password "{SECRET} con espacios"')
            assert changed["ok"] and _text(changed) == f"✔ alerts.smtp_password = {SECRET_MASK}"
            assert rt.settings.get("alerts.smtp_password") == f"{SECRET} con espacios"

            one_word = await execute(f"config providers.polygon_key={SECRET}")
            assert one_word["ok"] and _text(one_word) == f"✔ providers.polygon_key = {SECRET_MASK}"
            assert rt.settings.get("providers.polygon_key") == SECRET

            plain = await execute("settings nexus.horizon 7")
            assert plain["ok"] and _text(plain) == "✔ nexus.horizon = 7"

            slip = await execute(f"config set alerts.smtp_password {SECRET}")  # there is no `set` subcommand
            assert slip["ok"] is False and "Ajuste desconocido: set" in _text(slip) and SECRET not in _text(slip)

            await rt.bus.flush()
            logged = _logged(rt)
            assert not [m for m in logged if SECRET in m], logged
            assert any(m.startswith(f"> config {SECRET_MASK} (") for m in logged), logged
            assert any(m.startswith(f"> ajustes alerts.smtp_password {SECRET_MASK} (") for m in logged), logged
            assert any(m.startswith(f"> config providers.polygon_key {SECRET_MASK} (") for m in logged), logged
            assert any(m.startswith("> settings nexus.horizon 7 (") for m in logged), logged
            stored = await rt.db.fetchall("SELECT message FROM events WHERE source = 'terminal'")
            assert stored and not [r for r in stored if SECRET in r["message"]]

    asyncio.run(scenario())


def test_a_failing_settings_command_logs_no_credential(runtime_factory, monkeypatch):
    async def scenario():
        async with runtime_factory() as rt:
            async def broken(key, value):
                raise RuntimeError("base de datos bloqueada")

            monkeypatch.setattr(rt.settings, "set", broken)
            out = await execute(f"settings alerts.telegram_token 123456:{SECRET}")
            assert out["ok"] is False and "RuntimeError" in _text(out) and SECRET not in _text(out)
            errors = [e for e in rt.bus.recent(50, source="terminal") if e["level"] == "error"]
            assert errors and errors[-1]["message"].startswith(
                f"Fallo en comando 'settings alerts.telegram_token {SECRET_MASK}'")
            assert not [m for m in _logged(rt) if SECRET in m]

    asyncio.run(scenario())


def test_terminal_api_masks_credentials_too(api_client):
    r = api_client.post("/api/terminal", json={"text": f"settings alerts.smtp_password {SECRET}"})
    assert r.status_code == 200 and r.json()["ok"] and SECRET not in r.text
    listing = api_client.post("/api/terminal", json={"text": "settings"}).json()
    assert SECRET not in _text(listing)
    events = api_client.get("/api/events", params={"source": "terminal"}).json()["events"]
    assert events and not [e for e in events if SECRET in e["message"]]
