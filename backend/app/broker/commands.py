"""Terminal command of the practice-broker mirrors (group "Bróker"): `espejo` (Alpaca PAPER: its status and mirrored
positions), `espejo sincroniza` (reconcile now) and the same for Binance's testnets with `espejo binance` /
`espejo binance sincroniza`. Practice accounts only; the keys are never printed."""
from __future__ import annotations

from app.broker.mirror import MirrorError
from app.runtime import rt
from app.sim.common import fmt_num, fmt_price
from app.terminal import CommandContext, command

SYNC_WORDS = ("sincroniza", "sincronizar", "sync")
STATUS_WORDS = ("estado", "status")
BINANCE_WORDS = ("binance", "testnet")
USAGE = "Uso: espejo · espejo sincroniza · espejo binance · espejo binance sincroniza"


def _service(ctx: CommandContext, attr: str):
    svc = getattr(rt, attr, None)
    if svc is None or not hasattr(svc, "overview"):
        ctx.fail("El espejo del bróker de práctica no está disponible (el módulo no se cargó).")
    return svc


def _money(v, currency: str = "$") -> str:
    text = fmt_num(v, 2)
    return text if text == "—" else f"{text} {currency}"


@command("espejo", help="Espejo de NEXUS automática en tus cuentas de práctica (Alpaca paper o el testnet de "
                        "Binance): estado y posiciones",
         usage="espejo · espejo sincroniza · espejo binance · espejo binance sincroniza", group="Bróker",
         aliases=("alpaca", "broker"))
async def _espejo(ctx: CommandContext) -> None:
    words = [str(a).lower() for a in ctx.args]
    binance = bool(words) and words[0] in BINANCE_WORDS
    if binance:
        words = words[1:]
    if len(words) > 1 or (words and words[0] not in SYNC_WORDS + STATUS_WORDS):
        ctx.fail(USAGE)
    svc = _service(ctx, "broker_binance" if binance else "broker")
    venue = "el testnet de Binance" if binance else "Alpaca"
    if words and words[0] in SYNC_WORDS:
        try:
            st = await svc.sync_now()
        except MirrorError as exc:
            ctx.fail(str(exc))
    else:
        st = await svc.overview()
    ctx.data = st
    if not st["enabled"]:
        if binance:
            ctx.print(f"Espejo en {venue} desactivado: actívalo en Ajustes → Testnet de Binance (práctica) con las "
                      "claves de los testnets de contado y de futuros.", "muted")
        else:
            ctx.print("Espejo desactivado: actívalo en Ajustes → Bróker de práctica (Alpaca) con las claves de una "
                      "cuenta de práctica (paper).", "muted")
        return
    acct = st.get("account") or {}
    if st["connected"]:
        currency = str(acct.get("currency") or "USD")
        cur = "$" if currency == "USD" else currency
        label = "Binance Testnet (práctica)" if binance else "Alpaca (cuenta de práctica)"
        ctx.print(f"{label} conectada · capital {_money(acct.get('equity'), cur)} · efectivo "
                  f"{_money(acct.get('cash'), cur)}", "info")
    if st.get("error"):
        ctx.print(st["error"], "warn")
    ctx.print(st["stats"]["text"], "info")
    rows = [[e["symbol"], "largo" if e["side"] == "long" else "corto", e["status_label"], fmt_price(e["sim_entry"]),
             fmt_price(e["fill_entry"]),
             "—" if e["slip_entry_bps"] is None else f"{fmt_num(e['slip_entry_bps'], 1, signed=True)} pb",
             e.get("reason") or ""] for e in st["entries"][:10]]
    if rows:
        ctx.table(["Mercado", "Lado", "Estado", "NEXUS", "Binance" if binance else "Alpaca", "Diferencia", "Motivo"],
                  rows)
    ctx.print(st["note"], "muted")
