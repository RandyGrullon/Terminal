"""Spanish titles for the most common economic-calendar events (ForexFactory naming).

`{de}` is replaced by the region of the event currency ("de EE. UU.", "de la eurozona"...) and `{banco}` by its
central bank. Unknown titles keep the original (English) text, so nothing is ever hidden from the user.
"""
from __future__ import annotations

import re

REGION: dict[str, str] = {
    "USD": "de EE. UU.", "EUR": "de la eurozona", "GBP": "del Reino Unido", "JPY": "de Japón",
    "CAD": "de Canadá", "AUD": "de Australia", "NZD": "de Nueva Zelanda", "CHF": "de Suiza", "CNY": "de China",
    "MXN": "de México", "BRL": "de Brasil", "ALL": "mundial",
}
BANK: dict[str, str] = {
    "USD": "de la Fed", "EUR": "del BCE", "GBP": "del Banco de Inglaterra", "JPY": "del Banco de Japón",
    "CAD": "del Banco de Canadá", "AUD": "del RBA (Australia)", "NZD": "del RBNZ (Nueva Zelanda)",
    "CHF": "del SNB (Suiza)", "CNY": "del PBoC (China)",
}
# Country prefixes used by ForexFactory inside a currency block ("German Prelim CPI m/m" is a EUR event).
PREFIX_REGION: dict[str, str] = {
    "German": "de Alemania", "French": "de Francia", "Italian": "de Italia", "Spanish": "de España",
    "Tokyo": "de Tokio", "Chinese": "de China",
}
QUALIFIERS: dict[str, str] = {
    "Prelim": "preliminar", "Flash": "preliminar", "Final": "final", "Advance": "adelantado", "Revised": "revisado",
}

TITLES: dict[str, str] = {
    # Federal Reserve
    "FOMC Statement": "Comunicado de la Fed (FOMC)",
    "Federal Funds Rate": "Decisión de tipos de la Fed",
    "FOMC Press Conference": "Rueda de prensa del presidente de la Fed",
    "FOMC Meeting Minutes": "Actas de la Fed (FOMC)",
    "FOMC Economic Projections": "Proyecciones económicas de la Fed",
    # Other central banks
    "Main Refinancing Rate": "Decisión de tipos del BCE",
    "ECB Press Conference": "Rueda de prensa del BCE",
    "Monetary Policy Statement": "Comunicado de política monetaria {banco}",
    "Monetary Policy Meeting Accounts": "Actas de política monetaria {banco}",
    "Official Bank Rate": "Decisión de tipos del Banco de Inglaterra",
    "MPC Official Bank Rate Votes": "Votación de tipos del Banco de Inglaterra",
    "Monetary Policy Summary": "Resumen de política monetaria del Banco de Inglaterra",
    "BOJ Policy Rate": "Decisión de tipos del Banco de Japón",
    "BOJ Press Conference": "Rueda de prensa del Banco de Japón",
    "BOJ Outlook Report": "Informe de perspectivas del Banco de Japón",
    "Overnight Rate": "Decisión de tipos del Banco de Canadá",
    "BOC Rate Statement": "Comunicado del Banco de Canadá",
    "BOC Press Conference": "Rueda de prensa del Banco de Canadá",
    "BOC Monetary Policy Report": "Informe de política monetaria del Banco de Canadá",
    "Cash Rate": "Decisión de tipos del RBA (Australia)",
    "RBA Rate Statement": "Comunicado del RBA (Australia)",
    "RBA Press Conference": "Rueda de prensa del RBA (Australia)",
    "Official Cash Rate": "Decisión de tipos del RBNZ (Nueva Zelanda)",
    "RBNZ Rate Statement": "Comunicado del RBNZ (Nueva Zelanda)",
    "RBNZ Press Conference": "Rueda de prensa del RBNZ (Nueva Zelanda)",
    "SNB Policy Rate": "Decisión de tipos del SNB (Suiza)",
    "SNB Press Conference": "Rueda de prensa del SNB (Suiza)",
    # Inflation
    "CPI m/m": "IPC {de} (mensual)",
    "CPI y/y": "IPC {de} (anual)",
    "CPI q/q": "IPC {de} (trimestral)",
    "Core CPI m/m": "IPC subyacente {de} (mensual)",
    "Core CPI y/y": "IPC subyacente {de} (anual)",
    "Trimmed Mean CPI m/m": "IPC media recortada {de} (mensual)",
    "Trimmed Mean CPI q/q": "IPC media recortada {de} (trimestral)",
    "PPI m/m": "Precios al productor {de} (mensual)",
    "PPI y/y": "Precios al productor {de} (anual)",
    "Core PPI m/m": "Precios al productor subyacentes {de} (mensual)",
    "Core PCE Price Index m/m": "Inflación PCE subyacente {de} (mensual)",
    "Core PCE Price Index y/y": "Inflación PCE subyacente {de} (anual)",
    "PCE Price Index m/m": "Inflación PCE {de} (mensual)",
    "Employment Cost Index q/q": "Índice de costo laboral {de} (trimestral)",
    # Jobs
    "Non-Farm Employment Change": "Nóminas no agrícolas {de} (NFP)",
    "Unemployment Rate": "Tasa de desempleo {de}",
    "Average Hourly Earnings m/m": "Salario por hora {de} (mensual)",
    "ADP Non-Farm Employment Change": "Empleo privado ADP {de}",
    "ADP Weekly Employment Change": "Empleo privado ADP semanal {de}",
    "JOLTS Job Openings": "Ofertas de empleo JOLTS {de}",
    "Unemployment Claims": "Solicitudes de subsidio por desempleo {de}",
    "Employment Change": "Variación del empleo {de}",
    "Claimant Count Change": "Solicitudes de subsidio por desempleo {de}",
    "Unemployment Change": "Variación del desempleo {de}",
    "Average Earnings Index 3m/y": "Salarios medios {de} (3 meses, anual)",
    # Growth & activity
    "GDP q/q": "PIB {de} (trimestral)",
    "GDP m/m": "PIB {de} (mensual)",
    "GDP y/y": "PIB {de} (anual)",
    "GDP Price Index q/q": "Deflactor del PIB {de} (trimestral)",
    "Retail Sales m/m": "Ventas minoristas {de} (mensual)",
    "Core Retail Sales m/m": "Ventas minoristas subyacentes {de} (mensual)",
    "ISM Manufacturing PMI": "PMI manufacturero ISM {de}",
    "ISM Services PMI": "PMI de servicios ISM {de}",
    "Manufacturing PMI": "PMI manufacturero {de}",
    "Services PMI": "PMI de servicios {de}",
    "Chicago PMI": "PMI de Chicago {de}",
    "Non-Manufacturing PMI": "PMI no manufacturero {de}",
    "Caixin Manufacturing PMI": "PMI manufacturero Caixin {de}",
    "Caixin Services PMI": "PMI de servicios Caixin {de}",
    "Autumn Forecast Statement": "Previsiones fiscales de otoño {de}",
    "Spring Forecast Statement": "Previsiones fiscales de primavera {de}",
    "Empire State Manufacturing Index": "Índice manufacturero Empire State {de}",
    "Philly Fed Manufacturing Index": "Índice manufacturero de la Fed de Filadelfia",
    "Industrial Production m/m": "Producción industrial {de} (mensual)",
    "Durable Goods Orders m/m": "Pedidos de bienes duraderos {de} (mensual)",
    "Core Durable Goods Orders m/m": "Pedidos de bienes duraderos subyacentes {de} (mensual)",
    "Trade Balance": "Balanza comercial {de}",
    "Goods Trade Balance": "Balanza comercial de bienes {de}",
    "Personal Income m/m": "Ingresos personales {de} (mensual)",
    "Personal Spending m/m": "Gasto personal {de} (mensual)",
    # Sentiment & housing
    "CB Consumer Confidence": "Confianza del consumidor (Conference Board) {de}",
    "UoM Consumer Sentiment": "Sentimiento del consumidor (U. de Michigan)",
    "UoM Inflation Expectations": "Expectativas de inflación (U. de Michigan)",
    "ifo Business Climate": "Clima empresarial ifo {de}",
    "ZEW Economic Sentiment": "Sentimiento económico ZEW {de}",
    "Building Permits": "Permisos de construcción {de}",
    "Housing Starts": "Inicio de viviendas {de}",
    "Existing Home Sales": "Venta de viviendas usadas {de}",
    "New Home Sales": "Venta de viviendas nuevas {de}",
    "Pending Home Sales m/m": "Venta de viviendas pendientes {de} (mensual)",
    # Energy, fiscal, holidays
    "Crude Oil Inventories": "Inventarios de crudo {de}",
    "Natural Gas Storage": "Almacenamiento de gas natural {de}",
    "Annual Budget Release": "Presupuesto anual {de}",
    "Bank Holiday": "Festivo bancario {de}",
}

_SPEAKER = re.compile(r"^(?P<who>.+?)\s+(?P<name>[A-Z][\w'\-]+)\s+(?:Speaks|Testifies)$")
_SPEAKER_ROLES: dict[str, str] = {
    "Fed Chair": "presidente de la Fed", "Fed Vice Chair": "vicepresidente de la Fed",
    "FOMC Member": "miembro del FOMC", "ECB President": "presidenta del BCE", "ECB Member": "miembro del BCE",
    "BOE Gov": "gobernador del Banco de Inglaterra", "MPC Member": "miembro del Banco de Inglaterra",
    "BOJ Gov": "gobernador del Banco de Japón", "BOC Gov": "gobernador del Banco de Canadá",
    "RBA Gov": "gobernadora del RBA", "RBNZ Gov": "gobernadora del RBNZ", "SNB Chairman": "presidente del SNB",
}
_PAREN = re.compile(r"\s*\([^()]*\)\s*$")


def _fill(template: str, currency: str, region: str | None = None) -> str:
    de = region or REGION.get(currency, f"({currency})" if currency else "")
    banco = BANK.get(currency, f"({currency})" if currency else "")
    text = template.replace("{de}", de).replace("{banco}", banco)
    return " ".join(text.split())


def _lookup(title: str, currency: str, region: str | None = None) -> str | None:
    if title in TITLES:
        return _fill(TITLES[title], currency, region)
    word, _, rest = title.partition(" ")
    if word in QUALIFIERS and rest:
        base = TITLES.get(rest)
        if base:
            return _insert_qualifier(_fill(base, currency, region), QUALIFIERS[word])
    return None


def _insert_qualifier(text: str, qualifier: str) -> str:
    """'PIB de EE. UU. (trimestral)' + 'adelantado' -> 'PIB adelantado de EE. UU. (trimestral)'."""
    m = re.search(r"\s(de|del)\s", text)
    if m:
        return f"{text[:m.start()]} {qualifier}{text[m.start():]}"
    return f"{text} {qualifier}"


def spanish_title(title: str, currency: str = "") -> str:
    """Spanish title for a calendar event; falls back to the original title."""
    raw = " ".join(str(title or "").split())
    cur = str(currency or "").upper()
    if not raw:
        return ""
    found = _lookup(raw, cur)
    if found:
        return found
    m = _SPEAKER.match(raw)
    if m:
        role = _SPEAKER_ROLES.get(m.group("who"))
        if role:
            return f"Discurso de {m.group('name')} ({role})"
    word, _, rest = raw.partition(" ")
    if word in PREFIX_REGION and rest:
        found = _lookup(rest, cur, PREFIX_REGION[word])
        if found:
            return found
    return raw


def short_title(title_es: str) -> str:
    """Drop the trailing parenthesis: 'IPC de EE. UU. (mensual)' -> 'IPC de EE. UU.'."""
    text = _PAREN.sub("", str(title_es or "")).strip()
    return text or str(title_es or "")
