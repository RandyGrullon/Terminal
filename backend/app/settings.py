"""Persistent settings (table `settings`) merged over DEFAULT_SETTINGS, with type coercion."""
from __future__ import annotations

import copy
import json
from typing import Any, Callable

from app.config import DEFAULT_SETTINGS
from app.db import Database
from app.util import now_ms

_TRUE = {"1", "true", "yes", "si", "sí", "on", "y", "s"}
_FALSE = {"0", "false", "no", "off", "n", ""}


class SettingsStore:
    def __init__(self, db: Database):
        self.db = db
        self._values: dict[str, Any] = {}
        self._listeners: list[Callable[[str, Any], None]] = []

    async def load(self) -> None:
        rows = await self.db.fetchall("SELECT key, value FROM settings")
        for row in rows:
            try:
                self._values[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                continue

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return copy.deepcopy(self._values[key])
        if key in DEFAULT_SETTINGS:
            return copy.deepcopy(DEFAULT_SETTINGS[key])
        return default

    def all(self) -> dict[str, Any]:
        merged = copy.deepcopy(DEFAULT_SETTINGS)
        merged.update(copy.deepcopy(self._values))
        return merged

    async def set(self, key: str, value: Any) -> Any:
        value = self.coerce(key, value)
        self._values[key] = value
        await self.db.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now_ms()),
        )
        for cb in list(self._listeners):
            try:
                cb(key, value)
            except Exception:
                pass
        return value

    async def update(self, values: dict[str, Any]) -> dict[str, Any]:
        coerced = {k: self.coerce(k, v) for k, v in values.items()}  # validate everything first
        for k, v in coerced.items():
            await self.set(k, v)
        return self.all()

    def add_listener(self, cb: Callable[[str, Any], None]) -> None:
        """cb(key, value) is called after every change."""
        self._listeners.append(cb)

    @staticmethod
    def coerce(key: str, value: Any) -> Any:
        if key not in DEFAULT_SETTINGS:
            if key.startswith("ui."):
                return value
            raise KeyError(f"Ajuste desconocido: {key}")
        default = DEFAULT_SETTINGS[key]
        try:
            if isinstance(default, bool):
                if isinstance(value, bool):
                    return value
                s = str(value).strip().lower()
                if s in _TRUE:
                    return True
                if s in _FALSE:
                    return False
                raise ValueError
            if isinstance(default, int):
                return int(float(value))
            if isinstance(default, float):
                return float(value)
            if isinstance(default, list):
                if isinstance(value, str):
                    s = value.strip()
                    if s.startswith("["):
                        value = json.loads(s)
                    else:
                        value = [p.strip() for p in s.split(",") if p.strip()]
                if not isinstance(value, (list, tuple)):
                    raise ValueError
                if default and all(isinstance(x, (int, float)) for x in default):
                    return [float(x) for x in value]
                return list(value)
            if isinstance(default, str):
                return "" if value is None else str(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError(f"Valor no válido para {key}: {value!r}") from None
        return value
