"""Bundled PyChart example scripts.

Each example is a plain PyChart script stored next to this file (`*.py`, Spanish comments). They are NOT imported
as Python modules: `load_examples()` reads their source and ScriptService seeds them into table `scripts` with
`is_example=1` (refreshing the stored code whenever the file changes). The order below is the order in which
they are presented (simple -> advanced).
"""
from __future__ import annotations

from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent

# (file, name shown to the user, description)
EXAMPLES: tuple[tuple[str, str, str], ...] = (
    (
        "ema_cross.py",
        "Cruce de EMAs (estrategia)",
        "Estrategia clásica: compra cuando la EMA rápida cruza la lenta a favor de la tendencia mayor (EMA 200), "
        "con stop ATR, objetivo en R y cierre por cruce contrario.",
    ),
    (
        "rsi_divergence.py",
        "RSI con divergencias",
        "RSI con zonas de sobrecompra/sobreventa y detección causal de divergencias alcistas y bajistas "
        "entre pivotes confirmados del precio y del RSI.",
    ),
    (
        "supertrend_adx.py",
        "Supertrend + filtro ADX (estrategia)",
        "Sigue la tendencia con Supertrend (calculado paso a paso) y solo opera cuando el ADX confirma fuerza; "
        "el propio Supertrend actúa como stop dinámico.",
    ),
    (
        "bollinger_squeeze.py",
        "Squeeze de Bollinger",
        "Detecta la compresión de volatilidad (Bollinger dentro de Keltner) y la dirección de la ruptura "
        "con un histograma de momento.",
    ),
    (
        "macd_volume.py",
        "MACD + volumen",
        "Cruces del MACD confirmados por volumen relativo alto; histograma coloreado y alertas.",
    ),
    (
        "ichimoku.py",
        "Ichimoku completo",
        "Tenkan, Kijun, nube (Senkou A/B desplazadas) y Chikou, con señales de cruce TK filtradas por la nube.",
    ),
    (
        "vwap_bands.py",
        "Bandas VWAP",
        "VWAP de sesión diaria con bandas de 1 y 2 desviaciones ponderadas por volumen: zonas de valor y "
        "extensiones.",
    ),
    (
        "nexus_lite.py",
        "NEXUS-lite: mini ensamble con filtro de no operar",
        "Mini ensamble de 5 expertos (tendencia, MACD, RSI, Donchian, volumen) con un filtro que NO deja operar "
        "con ADX < 18, precio a más de 2.5 ATR de la EMA o volumen débil.",
    ),
    (
        "donchian_breakout.py",
        "Breakout de Donchian con stop ATR (estrategia)",
        "Ruptura del canal de Donchian (sistema tortuga) con stop inicial ATR, trailing ATR y salida por el "
        "canal corto.",
    ),
    (
        "sesiones_sydney_londres.py",
        "Rangos de sesión: Sídney → Asia, Londres → NY",
        "Marca el máximo y el mínimo de Sídney (para operar en Asia) y de Londres (para operar en Nueva York), las "
        "rupturas y barridas de esos niveles y las noticias principales (IPC, empleo, Fed); resume el historial "
        "como «x de n».",
    ),
    (
        "estrategia_rangos_sesion.py",
        "Rango de sesión: barrida o ruptura (estrategia)",
        "Reglas fijas sobre el rango de Londres o de Sídney: vende o compra tras una barrida (reversión) o una "
        "ruptura (continuación), con stop, objetivo, una operación por sesión, cierre al final de la ventana y "
        "filtro de noticias (IPC, empleo, Fed).",
    ),
)


def load_examples() -> list[dict]:
    """[{file, name, description, code}] in presentation order. Line endings are normalized to '\\n' so the
    stored code is identical on every platform (the seeder compares it to decide whether to refresh)."""
    out: list[dict] = []
    for file, name, description in EXAMPLES:
        code = (EXAMPLES_DIR / file).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        out.append({"file": file, "name": name, "description": description, "code": code})
    return out
