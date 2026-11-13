"""Terminal command of the practice-broker mirror (group "Bróker"): `espejo` (its status and mirrored positions) and
`espejo sincroniza` (reconcile now). Alpaca PAPER only; the keys are never printed."""
from __future__ import annotations

from app.broker.mirror import MirrorError
from app.runtime import rt
from app.sim.common import fmt_num, fmt_price
from app.terminal import CommandContext, command

SYNC_WORDS = ("sincroniza", "sincronizar", "sync")
STATUS_WORDS = ("estado", "status")


def _service(ctx: CommandContext):
    if getattr(rt, "broker", None) is None or not hasattr(rt.broker, "overview"):
        ctx.fail("El espejo del bróker de práctica no está disponible (el módulo no se cargó).")
    return rt.broker


def _money(v) -> str:
    text = fmt_num(v, 2)
    return text if text == "—" else f"{text} $"


@command("espejo", help="Espejo de NEXUS automática en tu cuenta de práctica de Alpaca (paper): estado y posiciones",
         usage="espejo · espejo sincroniza", group="Bróker", aliases=("alpaca", "broker"))
async def _espejo(ctx: CommandContext) -> None:
    svc = _service(ctx)
    word = str(ctx.args[0]).lower() if ctx.args else ""
    if word and word not in SYNC_WORDS + STATUS_WORDS:
        ctx.fail("Uso: espejo · espejo sincroniza")
    if word in SYNC_WORDS:
        try:
            st = await svc.sync_now()
        except MirrorError as exc:
            ctx.fail(str(exc))
    else:
        st = await svc.overview()
    ctx.data = st
    if not st["enabled"]:
        ctx.print("Espejo desactivado: actívalo en Ajustes → Bróker de práctica (Alpaca) con las claves de una cuenta "
                  "de práctica (paper).", "muted")
        return
    acct = st.get("account") or {}
    if st["connected"]:
        ctx.print(f"Alpaca (cuenta de práctica) conectada · capital {_money(acct.get('equity'))} · efectivo "
                  f"{_money(acct.get('cash'))}", "info")
    if st.get("error"):
        ctx.print(st["error"], "warn")
    ctx.print(st["stats"]["text"], "info")
    rows = [[e["symbol"], "largo" if e["side"] == "long" else "corto", e["status_label"], fmt_price(e["sim_entry"]),
             fmt_price(e["fill_entry"]),
             "—" if e["slip_entry_bps"] is None else f"{fmt_num(e['slip_entry_bps'], 1, signed=True)} pb",
             e.get("reason") or ""] for e in st["entries"][:10]]
    if rows:
        ctx.table(["Mercado", "Lado", "Estado", "NEXUS", "Alpaca", "Diferencia", "Motivo"], rows)
    ctx.print(st["note"], "muted")
