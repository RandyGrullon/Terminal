"""Terminal command of the copilot (group "Copiloto"): `ia <pregunta>` (one question, no history) and `ia estado`."""
from __future__ import annotations

from app.ai.ollama import OllamaError
from app.ai.service import AIError
from app.runtime import rt
from app.terminal import CommandContext, command

STATUS_WORDS = ("estado", "status")
TOOL_LABEL = {
    "estado_nexus_automatica": "estado de NEXUS automática",
    "decisiones_recientes": "decisiones recientes",
    "diario_de_operaciones": "diario de operaciones",
    "comparacion_con_el_azar": "comparación con el azar",
    "analisis_nexus": "análisis de NEXUS",
    "semaforo_y_noticias": "semáforo y noticias",
    "referencia_pychart": "referencia de PyChart",
    "probar_estrategia": "prueba de estrategia",
    "buscar_en_internet": "búsqueda en internet",
}


def _service(ctx: CommandContext):
    if getattr(rt, "ai", None) is None or not hasattr(rt.ai, "chat"):
        ctx.fail("El copiloto no está disponible (el módulo de IA no se cargó).")
    return rt.ai


@command("ia", help="Pregúntale al copiloto (IA local con Ollama): explica, resume e investiga; no opera",
         usage="ia <pregunta> · ia estado", group="Copiloto", aliases=("copiloto", "ai"))
async def _ia(ctx: CommandContext) -> None:
    svc = _service(ctx)
    words = [str(a) for a in ctx.args]
    if not words or (len(words) == 1 and words[0].lower() in STATUS_WORDS):
        st = await svc.check()
        ctx.data = st
        if not st["enabled"]:
            ctx.print("Copiloto desactivado: actívalo en Ajustes → Copiloto IA (Ollama).", "muted")
            return
        if st["reachable"]:
            models = ", ".join(st["models"]) or "ninguno"
            ctx.print(f"Ollama en {st['url']} · modelos: {models} · usa: {st['model'] or (st['models'] or ['—'])[0]}",
                      "info")
        if st.get("error"):
            ctx.print(st["error"], "warn")
        ctx.print("Búsqueda en internet: " + ("activada (Brave Search)" if st["search_ready"] else "no configurada"),
                  "muted")
        if not words:
            ctx.print("Pregunta con: ia <pregunta>, p. ej. ia ¿por qué no operó hoy NEXUS automática?", "muted")
        return
    question = " ".join(words)
    try:
        res = await svc.chat([{"role": "user", "content": question}])
    except (AIError, OllamaError) as exc:
        ctx.fail(str(exc))
    if res["tools"]:
        ctx.print("Consultó: " + ", ".join(TOOL_LABEL.get(t["name"], t["name"]) for t in res["tools"]), "muted")
    ctx.print(res["reply"], "info")
    ctx.print(f"{res['model']} · {res['duration_ms'] / 1000:.1f} s · IA local: puede equivocarse; no es asesoría "
              "financiera.", "muted")
    ctx.data = res
