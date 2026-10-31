"""Terminal commands of the market-data hub (group "Mercado"): search, quote, candles, markets, universe, watch,
chart, providers, realtime, recorder."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import config
from app.data.base import ProviderError
from app.data.symbols import MARKET_NAMES, valid_market
from app.terminal import CommandContext, command
from app.util import clamp, normalize_tf, safe_float

GROUP = "Mercado"
HISTORY_LABELS = {
    "full": "historial completo",
    "keyed": "proveedor con clave",
    "recorder": "grabado localmente",
    "synthetic": "sintético",
    "none": "sin datos",
}
SORT_ALIASES = {
    "cambio": "change", "change_pct": "change", "rating": "Recommend.All", "rsi": "RSI", "adx": "ADX",
    "precio": "close", "price": "close", "volumen": "volume", "semana": "Perf.W", "perf_w": "Perf.W",
    "mes": "Perf.1M", "perf_1m": "Perf.1M", "volatilidad": "Volatility.D", "cap": "market_cap_basic",
    "mcap": "market_cap_basic", "market_cap": "market_cap_basic",
}


# ----------------------------------------------------------------------------- helpers


def _hub(ctx: CommandContext) -> Any:
    hub = ctx.rt.hub
    if hub is None:
        ctx.fail("El servicio de datos de mercado no está disponible")
    return hub


def _tf(ctx: CommandContext, text: Any, default: str | None = "1h") -> str | None:
    if text is None:
        return default
    try:
        return normalize_tf(text)
    except ValueError as exc:
        ctx.fail(str(exc))
    return default


def _label(provider: Any) -> str:
    from app.data.hub import provider_label

    return provider_label(provider)


def fmt_price(x: Any) -> str:
    v = safe_float(x)
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.2f}"
    if a >= 1:
        return f"{v:.4f}"
    if a >= 0.01:
        return f"{v:.6f}"
    return f"{v:.8f}"


def fmt_pct(x: Any) -> str:
    v = safe_float(x)
    return "—" if v is None else f"{v:+.2f}%"


def fmt_num(x: Any) -> str:
    v = safe_float(x)
    if v is None:
        return "—"
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.2f}{suffix}"
    return f"{v:.2f}" if v % 1 else f"{v:.0f}"


def fmt_time(t: Any, tf: str) -> str:
    ts = safe_float(t)
    if ts is None:
        return "—"
    fmt = "%Y-%m-%d" if tf in ("1d", "1w") else "%Y-%m-%d %H:%M"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(fmt)


def fmt_rating(x: Any) -> str:
    v = safe_float(x)
    if v is None:
        return "—"
    if v >= 0.5:
        label = "Compra fuerte"
    elif v >= 0.1:
        label = "Compra"
    elif v > -0.1:
        label = "Neutral"
    elif v > -0.5:
        label = "Venta"
    else:
        label = "Venta fuerte"
    return f"{label} ({v:+.2f})"


def fmt_quick(score: Any, prob: Any) -> str:
    s, p = safe_float(score), safe_float(prob)
    if s is None:
        return "—"
    return f"{s:+.2f}" + (f" ({p * 100:.0f}%)" if p is not None else "")


def _short(text: Any, n: int = 34) -> str:
    s = str(text or "")
    return s if len(s) <= n else s[: n - 1] + "…"


# ----------------------------------------------------------------------------- search / quote / candles


@command("search", help="Busca símbolos en TradingView (acciones, cripto, forex, índices, futuros...)",
         usage="search <texto> [--market crypto] [--limit 20]", group=GROUP, aliases=("buscar",))
async def _search(ctx: CommandContext) -> None:
    hub = _hub(ctx)
    text = " ".join(ctx.args).strip()
    if not text:
        ctx.fail("Indica qué buscar. Ejemplo: search apple")
    market = ctx.flag("market")
    if market is not None and not valid_market(str(market).lower()):
        ctx.fail(f"Mercado no válido: {market}. Usa 'markets' para ver la lista")
    limit = int(clamp(ctx.flag_int("limit", 20) or 20, 1, 100))
    try:
        results = await hub.search(text, market=str(market).lower() if market else None, limit=limit)
    except ProviderError as exc:
        ctx.fail(str(exc))
    ctx.data = results
    if not results:
        ctx.print(f"Sin resultados para '{text}'", "warn")
        return
    rows = [[r["symbol"], _short(r.get("description")), r.get("type") or "—", fmt_price(r.get("price")),
             fmt_pct(r.get("change_pct")), HISTORY_LABELS.get(r.get("history"), r.get("history"))] for r in results]
    ctx.table(["Símbolo", "Descripción", "Tipo", "Precio", "Cambio", "Datos"], rows, max_rows=100)
    ctx.print("Ábrelo con: chart <símbolo> [tf]", "muted")


@command("quote", help="Cotización de uno o varios símbolos", usage="quote <símbolo> [símbolo...]", group=GROUP,
         aliases=("cotizacion", "cotización", "precio"))
async def _quote(ctx: CommandContext) -> None:
    hub = _hub(ctx)
    if not ctx.args:
        ctx.fail("Indica uno o varios símbolos. Ejemplo: quote btc eth aapl")
    symbols: list[str] = []
    for text in ctx.args[:30]:
        sym = await ctx.symbol(text)
        if sym not in symbols:
            symbols.append(sym)
    quotes = await hub.quotes(symbols)
    ctx.data = quotes
    from app.data.realtime import freshness_label

    rows = []
    for sym in symbols:
        q = quotes.get(sym) or {}
        rows.append([sym, fmt_price(q.get("price")), fmt_pct(q.get("change_pct")), fmt_price(q.get("high")),
                     fmt_price(q.get("low")), fmt_num(q.get("volume")), freshness_label(q or None, symbol=sym)])
    ctx.table(["Símbolo", "Precio", "Cambio", "Máximo", "Mínimo", "Volumen", "Origen"], rows)
    missing = [s for s in symbols if s not in quotes]
    if missing:
        ctx.print(f"Sin cotización disponible: {', '.join(missing)}", "warn")
    stale = [s for s in symbols if (quotes.get(s) or {}).get("stale")]
    if stale:
        ctx.print(f"Precio desactualizado (último dato guardado, no es un precio en vivo): {', '.join(stale)}", "warn")


@command("candles", help="Últimas velas de un símbolo y de dónde salen los datos",
         usage="candles <símbolo> [tf] [--limit 20]", group=GROUP, aliases=("velas",))
async def _candles(ctx: CommandContext) -> None:
    hub = _hub(ctx)
    symbol = await ctx.symbol(ctx.arg(0))
    tf = _tf(ctx, ctx.arg(1), "1h") or "1h"
    n = int(clamp(ctx.flag_int("limit", 20) or 20, 1, 200))
    try:
        cs = await hub.get_candles(symbol, tf, max(n + 1, 2))
    except ProviderError as exc:
        ctx.fail(str(exc))
    history = HISTORY_LABELS.get(cs.history, cs.history)
    ctx.print(f"{symbol} {tf} · {len(cs)} velas · fuente: {_label(cs.provider)} ({history})", "system")
    if cs.note:
        ctx.print(cs.note, "warn")
    candles = cs.to_list()
    ctx.data = {"symbol": symbol, "tf": tf, "provider": cs.provider, "history": cs.history, "note": cs.note,
                "stale": bool(getattr(cs, "stale", False)), "candles": candles[-n:]}
    if not candles:
        return
    rows = []
    for i in range(max(0, len(candles) - n), len(candles)):
        c = candles[i]
        prev = candles[i - 1]["close"] if i > 0 else None
        change = (c["close"] - prev) / prev * 100.0 if prev else None
        rows.append([fmt_time(c["time"], tf), fmt_price(c["open"]), fmt_price(c["high"]), fmt_price(c["low"]),
                     fmt_price(c["close"]), fmt_num(c["volume"]), fmt_pct(change)])
    ctx.table(["Hora (UTC)", "Apertura", "Máximo", "Mínimo", "Cierre", "Volumen", "Cambio"], rows, max_rows=200)


# ----------------------------------------------------------------------------- markets / universe


@command("markets", help="Mercados de TradingView y número de activos", usage="markets", group=GROUP,
         aliases=("mercados",))
async def _markets(ctx: CommandContext) -> None:
    hub = _hub(ctx)
    markets = await hub.markets()
    ctx.data = markets
    rows = [[m["id"], m["name"], f"{m['count']:,}".replace(",", ".") if m.get("count") is not None else "—"]
            for m in markets]
    ctx.table(["Mercado", "Nombre", "Activos"], rows, max_rows=100)
    if all(m.get("count") is None for m in markets):
        ctx.print("Recuentos no disponibles (sin conexión o TradingView desactivado)", "muted")
    ctx.print("Explora uno con: universe <mercado> [--sort change] [--limit 30]", "muted")


@command("universe", help="Activos de un mercado ordenados por volumen, cambio, rating, RSI...",
         usage="universe [mercado] [--sort volume|change|rating|rsi|adx|semana|mes] [--order asc|desc] "
               "[--limit 25] [--offset 0] [--q texto] [--type stock]",
         group=GROUP, aliases=("universo",))
async def _universe(ctx: CommandContext) -> None:
    hub = _hub(ctx)
    market = str(ctx.arg(0) or "crypto").lower()
    if not valid_market(market):
        ctx.fail(f"Mercado no válido: {market}. Usa 'markets' para ver la lista")
    sort = str(ctx.flag("sort", "volume") or "volume")
    sort = SORT_ALIASES.get(sort.lower(), sort)
    order = "asc" if str(ctx.flag("order", "desc")).lower() == "asc" else "desc"
    limit = int(clamp(ctx.flag_int("limit", 25) or 25, 1, 200))
    offset = max(0, ctx.flag_int("offset", 0) or 0)
    query = ctx.flag("q")
    kind = ctx.flag("type")
    try:
        data = await hub.universe(market, offset=offset, limit=limit, sort=sort, order=order,
                                  q=str(query) if query not in (None, True) else None,
                                  type_=str(kind).lower() if kind not in (None, True) else None)
    except ProviderError as exc:
        ctx.fail(str(exc))
    ctx.data = data
    rows = data.get("rows") or []
    name = MARKET_NAMES.get(market, market)
    if not rows:
        ctx.print(f"{name}: sin activos" + (" (modo sin conexión: solo símbolos guardados)" if config.OFFLINE else ""), "warn")
        return
    ctx.print(f"{name}: {data.get('total', len(rows)):,} activos · {offset + 1}–{offset + len(rows)} · orden {sort} {order}"
              .replace(",", "."), "system")
    table = [[r["symbol"], _short(r.get("description"), 28), fmt_price(r.get("close")), fmt_pct(r.get("change_pct")),
              fmt_num(r.get("volume")), fmt_rating(r.get("rating")),
              "—" if r.get("rsi") is None else f"{r['rsi']:.1f}", fmt_quick(r.get("quick_score"), r.get("quick_prob"))]
             for r in rows]
    ctx.table(["Símbolo", "Descripción", "Precio", "Cambio", "Volumen", "Rating TV", "RSI", "NEXUS (P↑)"], table,
              max_rows=200)


# ----------------------------------------------------------------------------- watchlist / chart


@command("watch", help="Lista de seguimiento: ver, añadir o quitar símbolos", usage="watch [list|add|rm] [símbolo...]",
         group=GROUP, aliases=("vigilar", "watchlist", "seguimiento"))
async def _watch(ctx: CommandContext) -> None:
    hub = _hub(ctx)
    sub = str(ctx.arg(0) or "list").lower()
    targets = ctx.args[1:]
    if sub in ("add", "añadir", "anadir", "agregar", "+"):
        if not targets:
            ctx.fail("Indica el símbolo. Ejemplo: watch add aapl")
        for text in targets:
            sym = await ctx.symbol(text)
            await hub.watch_add(sym)
            ctx.print(f"✔ {sym} añadido a la lista de seguimiento", "success")
    elif sub in ("rm", "remove", "del", "quitar", "borrar", "-"):
        if not targets:
            ctx.fail("Indica el símbolo. Ejemplo: watch rm aapl")
        for text in targets:
            sym = await ctx.symbol(text)
            if sym not in await hub.watchlist():
                ctx.print(f"{sym} no está en la lista de seguimiento", "warn")
                continue
            await hub.watch_remove(sym)
            ctx.print(f"✔ {sym} quitado de la lista de seguimiento", "success")
    elif sub not in ("list", "ls", "ver", "lista"):
        ctx.fail("Uso: watch [list|add|rm] [símbolo...]")
    items = await hub.watch_items()
    ctx.data = items
    if not items:
        ctx.print("La lista de seguimiento está vacía. Añade símbolos con: watch add btc", "muted")
        return
    rows = []
    for i, it in enumerate(items, 1):
        q = it.get("quote") or {}
        rows.append([i, it["symbol"], _short(it.get("description"), 30), fmt_price(q.get("price")),
                     fmt_pct(q.get("change_pct")), HISTORY_LABELS.get(it.get("history"), it.get("history"))])
    ctx.table(["#", "Símbolo", "Descripción", "Precio", "Cambio", "Datos"], rows, max_rows=200)


@command("chart", help="Abre el gráfico de un símbolo", usage="chart <símbolo> [tf]", group=GROUP,
         aliases=("grafico", "gráfico", "abrir"))
async def _chart(ctx: CommandContext) -> None:
    _hub(ctx)
    symbol = await ctx.symbol(ctx.arg(0))
    tf = _tf(ctx, ctx.arg(1), None)
    if tf:
        ctx.action("open_chart", symbol=symbol, tf=tf)
    else:
        ctx.action("open_chart", symbol=symbol)
    ctx.print(f"Abriendo el gráfico de {symbol}" + (f" en {tf}" if tf else ""), "success")
    ctx.data = {"symbol": symbol, "tf": tf}


# ----------------------------------------------------------------------------- providers / recorder


@command("providers", help="Fuentes de datos: exchanges, proveedores con clave, TradingView y grabador",
         usage="providers", group=GROUP, aliases=("proveedores", "fuentes"))
async def _providers(ctx: CommandContext) -> None:
    hub = _hub(ctx)
    st = hub.providers_status()
    rec = hub.recorder.status()
    online = not st.get("offline")
    rows: list[list[Any]] = [
        ["Cripto (exchanges)", ", ".join(_label(p) for p in st.get("crypto", [])),
         "✔ activo" if online else "✖ sin conexión"],
        ["Exchange preferido", _label(st.get("crypto_default")), "providers.crypto_default"],
    ]
    realtime = getattr(hub, "realtime", None)
    live = {p["id"]: p for p in (realtime.status().get("providers") or [])} if realtime is not None else {}
    for name, configured in (st.get("keyed") or {}).items():
        detail = "✔ acciones/forex/índices" if configured else f"providers.{name}_key"
        state = (live.get(name) or {}).get("state")
        if configured and state:
            detail += f" · tiempo real: {RT_STATES.get(state, state)}"
        rows.append([_label(name), "clave configurada" if configured else "sin clave", detail])
    tv = st.get("tradingview") or {}
    tv_state = "activo" if tv.get("enabled") and online else ("desactivado" if not tv.get("enabled") else "sin conexión")
    tv_detail = tv.get("error") or ", ".join(f"{k}={v}" for k, v in tv.items() if k not in ("enabled", "error"))[:60]
    rows.append(["TradingView", tv_state, tv_detail or "búsqueda, cotizaciones, escáner"])
    rows.append(["Grabador local", "activado" if rec.get("enabled") else "desactivado",
                 f"cada {rec.get('interval_s', 0):.0f} s · {rec.get('symbols', 0)} símbolos"])
    ctx.table(["Fuente", "Estado", "Detalle"], rows)
    status = hub.status()
    ctx.print(f"Suscripciones en vivo: {status['subscriptions']} · aciertos de caché: {status['cache_hits']} · "
              f"descargas: {status['fetches']} · errores: {status['fetch_errors']}", "muted")
    ctx.print("Configura una clave con: settings providers.twelvedata_key <clave> · detalle: realtime", "muted")
    ctx.data = {**st, "recorder": rec}


RT_STATES = {
    "sin_clave": "sin clave", "sin_tiempo_real": "no (plan gratuito de fin de día)", "en_espera": "en espera",
    "activo": "activo", "limite": "límite alcanzado", "plan": "tu plan no lo incluye", "error": "con errores",
}


@command("realtime", help="Cotizaciones en tiempo real con tus claves: qué es tiempo real en cada plan y consumo",
         usage="realtime", group=GROUP, aliases=("tiemporeal", "tiempo-real", "rt"))
async def _realtime(ctx: CommandContext) -> None:
    hub = _hub(ctx)
    service = getattr(hub, "realtime", None)
    if service is None:
        ctx.fail("El servicio de cotizaciones en tiempo real no está disponible")
    st = service.status()
    ctx.data = st
    state = "activado" if st["enabled"] else ("modo sin conexión" if st["offline"] else "desactivado (realtime.enabled)")
    ctx.print(f"Tiempo real con proveedores con clave · {state}", "system")
    rows = []
    for p in st["providers"]:
        used = f"{p['used_min']}/{p['credits_min']} min" if p["credits_min"] else "—"
        if p["credits_day"]:
            used += f" · {p['used_day']}/{p['credits_day']} día"
        rows.append([p["label"], RT_STATES.get(p["state"], p["state"]), used, p["detail"]])
    ctx.table(["Proveedor", "Estado", "Consumo", "Detalle"], rows)
    for p in st["providers"]:
        ctx.print(f"{p['label']}: {p['plan']} ({p['doc_url']})", "muted")
    if st["symbols"]:
        table = []
        for item in st["symbols"]:
            fresh = ("tiempo real" if item.get("realtime") else
                     "mercado cerrado" if item.get("market_open") is False else
                     f"retraso {round((item.get('delay_s') or 0) / 60)} min" if item.get("delay_s") else "pendiente")
            every = f"cada {item['interval_s']:.0f} s" if item.get("interval_s") else "—"
            table.append([item["symbol"], _label(item["provider"]), every, fresh])
        ctx.table(["Símbolo", "Proveedor", "Cadencia", "Último precio"], table)
    else:
        ctx.print("Ningún símbolo en tiempo real ahora: se consultan solo los gráficos abiertos de acciones o forex y "
                  "las posiciones demo abiertas (para no gastar los créditos del plan).", "muted")
    ctx.print("Sin clave (o si el plan no lo incluye) los precios de acciones y forex vienen de TradingView con el "
              "retraso que anuncia (p. ej. 15 min para NASDAQ/NYSE).", "muted")


@command("recorder", help="Grabador local de velas (activos sin API de velas gratuita)",
         usage="recorder [status|on|off|now]", group=GROUP, aliases=("grabador",))
async def _recorder(ctx: CommandContext) -> None:
    hub = _hub(ctx)
    rec = hub.recorder
    sub = str(ctx.arg(0) or "status").lower()
    if sub in ("on", "off", "activar", "desactivar"):
        enabled = sub in ("on", "activar")
        await ctx.rt.settings.set("recorder.enabled", enabled)  # the hub's settings listener starts/stops it
        ctx.print("✔ Grabador local activado" if enabled else "Grabador local desactivado", "success" if enabled else "warn")
    elif sub in ("now", "ahora", "run", "ya"):
        if config.OFFLINE:
            ctx.fail("Modo sin conexión: el grabador no puede consultar TradingView")
        try:
            res = await rec.run_once()
        except Exception as exc:
            ctx.fail(f"El ciclo del grabador falló: {exc}")
        level = "warn" if res.get("error") else "success"
        ctx.print(f"Ciclo manual: {res['bars']} velas de {res['symbols']} símbolos"
                  + (f" · {res['error']}" if res.get("error") else ""), level)
    elif sub not in ("status", "estado"):
        ctx.fail("Uso: recorder [status|on|off|now]")
    st = rec.status()
    ctx.print(f"Grabador local: {'activado' if st['enabled'] else 'desactivado'} · "
              f"{'en marcha' if st['running'] else 'detenido'} · cada {st['interval_s']:.0f} s", "system")
    ctx.print(f"Ciclos: {st['cycles']} · velas guardadas: {st['bars']} · instantáneas repetidas ignoradas: "
              f"{st['skipped_stale']} · cambios de vela de TradingView descartados: {st.get('skipped_rollover', 0)}")
    lags = st.get("lag_s") or {}
    if lags:
        shown = ", ".join(f"{s} {v:+.0f} s" for s, v in list(lags.items())[:10])
        ctx.print(f"Retraso real de TradingView respecto al anunciado (estimado por los cambios de vela): {shown}"
                  + (f" · correcciones: {st.get('corrections', 0)} ({st.get('relocated', 0)} velas recolocadas)"
                     if st.get("corrections") else ""), "muted")
    if st["recording"]:
        ctx.print("Grabando: " + ", ".join(st["recording"]))
    pending = [s for s in await rec.symbols_to_record() if s not in st["recording"]]
    if pending:
        ctx.print("Pendientes (lista de seguimiento o gráficos abiertos): " + ", ".join(pending[:20]), "muted")
    if st.get("last_error"):
        ctx.print(f"Último error: {st['last_error']}", "warn")
    ctx.data = st
