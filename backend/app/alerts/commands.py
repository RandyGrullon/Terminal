"""Terminal commands of the alerts feature (group "Alertas"): `alertas` (status) and `alertas test` (test message)."""
from __future__ import annotations

from app.alerts.channels import join_es
from app.alerts.classify import CATEGORY_INLINE, PRIORITY_LABEL
from app.alerts.service import HARD_CAP_PER_HOUR
from app.runtime import rt
from app.terminal import CommandContext, command
from app.util import now_ms

STATUS_LABEL = {
    "sent": "enviado",
    "partial": "enviado en parte",
    "failed": "falló",
    "quiet": "silenciado",
    "rate_limited": "por el límite",
    "no_channel": "sin canal",
}
STATUS_LEVEL = {"sent": "success", "partial": "warn", "failed": "error"}
TEST_WORDS = ("test", "prueba", "probar")


def _service(ctx: CommandContext):
    if rt.alerts is None or not hasattr(rt.alerts, "full_status"):
        ctx.fail("Las alertas externas no están disponibles (el módulo de alertas no se cargó).")
    return rt.alerts


def ago(ms: int | float | None) -> str:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return "—"
    s = max(0, int((now_ms() - ms) / 1000))
    if s < 60:
        return "hace un momento"
    if s < 3600:
        return f"hace {s // 60} min"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"hace {h} h {m} min" if m else f"hace {h} h"
    return f"hace {s // 86400} d"


def _channel_line(ch: dict) -> tuple[str, str]:
    if ch.get("active"):
        mark, level, state = "✓", "success", "activo"
    elif ch.get("configured"):
        mark, level, state = "○", "muted", "configurado pero desactivado"
    elif ch.get("enabled"):
        mark, level, state = "✗", "warn", "activado pero incompleto"
    else:
        mark, level, state = "○", "muted", "desactivado"
    parts = [f"{mark} {ch.get('label')}", state]
    if ch.get("target"):
        parts.append(str(ch["target"]))
    if ch.get("problem"):
        parts.append(str(ch["problem"]))
        level = "warn" if level != "success" else level
    elif ch.get("missing"):
        parts.append("falta " + join_es(list(ch["missing"])))
    last = ch.get("last")
    if isinstance(last, dict):
        if last.get("ok"):
            parts.append(f"último envío correcto {ago(last.get('at'))}")
        else:
            parts.append(f"último error {ago(last.get('at'))}: {last.get('error')}")
            level = "warn"
    return "  " + " · ".join(parts), level


@command("alertas", help="Alertas fuera de la app (Telegram, correo): estado o mensaje de prueba",
         usage="alertas [test]", group="Alertas", aliases=("alerts", "avisos"))
async def _alertas(ctx: CommandContext) -> None:
    svc = _service(ctx)
    sub = str(ctx.arg(0, "") or "").strip().lower()
    if sub in TEST_WORDS:
        await _test(ctx, svc)
        return
    if sub and sub not in ("estado", "status"):
        ctx.fail(f"Subcomando desconocido: {sub}. Usa: alertas · alertas test")
    st = svc.full_status()
    ctx.data = st
    ctx.print("Alertas fuera de la app (Telegram y correo)", "info")
    for warning in st.get("warnings") or []:
        ctx.print(f"⚠ {warning}", "warn")
    ctx.print("Canales:", "muted")
    for ch in st.get("channels") or []:
        text, level = _channel_line(ch)
        ctx.print(text, level)
    cats = [CATEGORY_INLINE.get(c, c) for c, on in (st.get("categories") or {}).items() if on]
    limit = st.get("max_per_hour")
    ceiling = max(2 * limit, HARD_CAP_PER_HOUR) if isinstance(limit, int) else HARD_CAP_PER_HOUR
    ctx.print(f"Qué se envía: {', '.join(cats) if cats else 'nada (categorías desactivadas)'} · nivel: "
              f"{str(PRIORITY_LABEL.get(st.get('min_level'), st.get('min_level'))).lower()} · máx. {limit} mensajes por "
              f"hora ({st.get('sent_last_hour')} en la última hora; los críticos pueden superarlo, hasta {ceiling})")
    if st.get("signal_symbols"):
        ctx.print(f"Señales solo de: {', '.join(st['signal_symbols'])}", "muted")
    q = st.get("quiet") or {}
    if q.get("enabled"):
        extra = " · ahora en silencio" if q.get("active") else ""
        crit = " · los críticos sí se envían" if q.get("critical") else ""
        ctx.print(f"Horas de silencio: de {q.get('start')} a {q.get('end')} ({q.get('timezone')}){crit}{extra}")
    else:
        ctx.print("Horas de silencio: no", "muted")
    c = st.get("counters") or {}
    ctx.print(f"Desde {ago(st.get('since'))}: {c.get('received', 0)} recibidos · {c.get('sent', 0)} enviados · "
              f"{c.get('failed', 0)} fallidos · {c.get('filtered', 0)} filtrados · {c.get('deduped', 0)} repetidos · "
              f"{c.get('quiet', 0)} silenciados · {c.get('rate_limited', 0)} por el límite · "
              f"{c.get('no_channel', 0)} sin canal", "muted")
    recent = list(st.get("recent") or [])[:8]
    if recent:
        ctx.print("")
        ctx.table(["Cuándo", "Estado", "Aviso"],
                  [[ago(r.get("at")), STATUS_LABEL.get(r.get("status"), r.get("status")), r.get("text")]
                   for r in recent])
    if not st.get("active_channels"):
        ctx.print("Ningún canal activo: configúralos en Ajustes → Alertas y prueba con «alertas test».", "warn")


async def _test(ctx: CommandContext, svc) -> None:
    from app.alerts.service import AlertBusy

    try:
        result = await svc.send_test(None)
    except AlertBusy as exc:
        ctx.fail(str(exc))
        return
    ctx.data = result
    for r in result.get("results") or []:
        if r.get("skipped"):
            ctx.print(f"○ {r.get('label')}: no se probó · {r.get('error')}", "muted")
        elif r.get("ok"):
            off = "" if r.get("enabled") else " (canal desactivado: actívalo para recibir los avisos)"
            ctx.print(f"✓ {r.get('label')}: mensaje de prueba enviado en {r.get('duration_ms', 0)} ms{off}", "success")
        else:
            ctx.print(f"✗ {r.get('label')}: {r.get('error')}", "error")
    if not any(not r.get("skipped") for r in result.get("results") or []):
        ctx.print(result.get("message") or "No hay ningún canal configurado.", "warn")
    if not result.get("ok"):
        ctx.fail("La prueba no se completó en todos los canales configurados.")
