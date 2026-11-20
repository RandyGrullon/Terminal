"""How the server itself is doing (`rt.sysmon`), for the simple mode's «Servidor» tab: where it runs (this PC, a phone
with Termux, a cloud machine), since when, every start with how long it was down before (a crash or a power cut leaves
no clean stop), the internet as the server sees it (a light probe of a public market-data endpoint once a minute), the
battery when the machine has one, the process memory and the database size.

The heartbeat (meta `system.heartbeat`, every HEARTBEAT_S) is what tells a crash from a clean stop: at start, the time
since the last heartbeat is the time it was down. The starts ledger is kept in meta `system.starts` (the last
KEEP_STARTS). Nothing here changes how NEXUS trades.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from app import config
from app.util import RateLimitedLog, http_client, now_ms

HEARTBEAT_KEY = "system.heartbeat"
STARTS_KEY = "system.starts"
HEARTBEAT_S = 30
PROBE_S = 60
KEEP_STARTS = 30
KEEP_PROBES = 60
PROBE_URL = "https://data-api.binance.vision/api/v3/ping"  # Binance's public market-data host (the gold and crypto feed)
PROBE_LABEL = "datos de mercado (Binance)"
SLOW_MS = 1500
BATTERY_TTL_S = 60


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return None


def is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "") or Path("/data/data/com.termux").exists()


def host_info() -> dict[str, Any]:
    """Where the server runs: {kind: android | windows | linux | mac, label (Spanish), device, python}."""
    py = platform.python_version()
    if is_termux():
        model = None
        try:
            model = subprocess.run(["getprop", "ro.product.model"], capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception:
            pass
        device = (model or "Android").replace("_", " ")
        return {"kind": "android", "label": f"Teléfono {device} (Termux)", "device": device, "python": py}
    if os.name == "nt":
        name = platform.node() or "Windows"
        return {"kind": "windows", "label": f"PC con Windows ({name})", "device": name, "python": py}
    if sys.platform == "darwin":
        return {"kind": "mac", "label": "Mac", "device": platform.node() or "Mac", "python": py}
    in_container = Path("/.dockerenv").exists() or bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("FLY_APP_NAME"))
    return {"kind": "linux", "label": "Servidor en la nube" if in_container else f"Linux ({platform.node()})",
            "device": platform.node() or "linux", "python": py}


def memory_mb() -> float | None:
    """Resident memory of this process in MB."""
    status = _read("/proc/self/status")
    if status:
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                try:
                    return round(int(line.split()[1]) / 1024.0, 1)
                except (IndexError, ValueError):
                    return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE  # a 64-bit pseudo-handle: never an int
            k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
            if k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return round(pmc.WorkingSetSize / 1048576.0, 1)
        except Exception:
            return None
    return None


def parse_termux_battery(raw: str) -> dict[str, Any] | None:
    """`termux-battery-status` JSON -> Battery."""
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("percentage") is None:
        return None
    status = str(d.get("status") or "").upper()
    return {"percent": float(d["percentage"]), "charging": status in ("CHARGING", "FULL") or d.get("plugged") not in (None, "UNPLUGGED"),
            "plugged": d.get("plugged") not in (None, "UNPLUGGED"),
            "temperature_c": float(d["temperature"]) if d.get("temperature") is not None else None,
            "health": str(d.get("health") or "") or None, "source": "termux-api"}


def battery_info() -> tuple[dict[str, Any] | None, str | None]:
    """(Battery, None) or (None, why it is not known). Android: sysfs, else Termux:API; Windows: the system's power
    status; Linux: sysfs."""
    for base in ("/sys/class/power_supply/battery", "/sys/class/power_supply/BAT0", "/sys/class/power_supply/BAT1"):
        cap = _read(f"{base}/capacity")
        if cap is not None:
            try:
                status = (_read(f"{base}/status") or "").lower()
                temp = _read(f"{base}/temp")
                return ({"percent": float(cap), "charging": status in ("charging", "full"), "plugged": status != "discharging",
                         "temperature_c": round(int(temp) / 10.0, 1) if temp and temp.lstrip("-").isdigit() else None,
                         "health": _read(f"{base}/health"), "source": "sysfs"}, None)
            except ValueError:
                pass
    if is_termux():
        exe = shutil.which("termux-battery-status")
        if not exe:
            return None, ("Android no deja leer la batería sin la app Termux:API: instálala y ejecuta "
                          "«pkg install termux-api» en Termux")
        try:
            out = subprocess.run([exe], capture_output=True, text=True, timeout=8).stdout
        except Exception:
            out = ""
        info = parse_termux_battery(out)
        return (info, None) if info else (None, "Termux:API no respondió (¿está instalada la app Termux:API?)")
    if os.name == "nt":
        try:
            import ctypes

            class SPS(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                            ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                            ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

            s = SPS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
                flag = s.BatteryFlag & 0xFF
                pct = s.BatteryLifePercent & 0xFF
                if flag == 128 or pct == 255:
                    return None, "esta PC no tiene batería: si se va la luz, se apaga"
                return ({"percent": float(pct), "charging": bool(flag & 8), "plugged": s.ACLineStatus == 1,
                         "temperature_c": None, "health": None, "source": "windows"}, None)
        except Exception:
            pass
    return None, "no se puede leer la batería en esta máquina"


class SystemMonitor:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self._tasks: list[asyncio.Task] = []
        self._probes: deque[dict[str, Any]] = deque(maxlen=KEEP_PROBES)
        self._starts: list[dict[str, Any]] = []
        self._battery: tuple[float, dict | None, str | None] | None = None
        self._log = RateLimitedLog()
        self._host = host_info()
        self._net_down_since: int | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self._record_start()
        if config.BACKGROUND:
            self._tasks = [asyncio.create_task(self._beat_loop(), name="sysmon-heartbeat")]
            if not config.OFFLINE:
                self._tasks.append(asyncio.create_task(self._probe_loop(), name="sysmon-probe"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        await self._heartbeat(clean=True)

    def status(self) -> dict[str, Any]:
        last = self._probes[-1] if self._probes else None
        return {"running": True, "host": self._host["kind"], "net_ok": None if last is None else last["ok"]}

    # ------------------------------------------------------------------ state

    async def _meta_get(self, key: str) -> Any:
        try:
            raw = await self.rt.db.fetchval("SELECT value FROM meta WHERE key=?", (key,))
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _meta_put(self, key: str, value: Any) -> None:
        try:
            await self.rt.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))
        except Exception as exc:
            if self._log.should_log(f"meta:{key}", 600):
                self._emit("warn", f"Estado del servidor: no se pudo guardar {key}: {exc}")

    def _emit(self, level: str, message: str, data: Any = None) -> None:
        bus = getattr(self.rt, "bus", None)
        if bus is not None:
            bus.emit(level, "system", message, data)

    async def _record_start(self) -> None:
        """This start, with how long the server was down before it and whether the last stop was clean."""
        beat = await self._meta_get(HEARTBEAT_KEY)
        starts = await self._meta_get(STARTS_KEY)
        self._starts = [s for s in starts if isinstance(s, dict)] if isinstance(starts, list) else []
        now = now_ms()
        entry: dict[str, Any] = {"at": now, "host": self._host["label"], "down_ms": None, "clean": None, "last_seen": None}
        if isinstance(beat, dict) and isinstance(beat.get("ts"), (int, float)):
            entry["last_seen"] = int(beat["ts"])
            entry["down_ms"] = max(0, now - int(beat["ts"]))
            entry["clean"] = bool(beat.get("clean"))
        self._starts = [*self._starts, entry][-KEEP_STARTS:]
        await self._meta_put(STARTS_KEY, self._starts)
        await self._heartbeat()
        if entry["down_ms"] is not None and entry["clean"] is False and entry["down_ms"] > 2 * HEARTBEAT_S * 1000:
            self._emit("warn", f"El servidor estuvo caído {fmt_span(entry['down_ms'] / 1000)} sin apagarse bien "
                               "(apagón, cierre forzado o el sistema lo detuvo); ya está funcionando otra vez",
                       {"down_ms": entry["down_ms"]})

    async def _heartbeat(self, clean: bool = False) -> None:
        await self._meta_put(HEARTBEAT_KEY, {"ts": now_ms(), "clean": clean})

    async def _beat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self._heartbeat()

    # ------------------------------------------------------------------ internet

    async def probe(self) -> dict[str, Any]:
        """One light request to the public market-data host: ok, milliseconds, error (Spanish)."""
        t0 = time.monotonic()
        sample: dict[str, Any] = {"ts": now_ms(), "ok": False, "ms": None, "error": None}
        try:
            async with http_client(timeout=10.0) as client:
                r = await client.get(PROBE_URL)
            sample["ms"] = round((time.monotonic() - t0) * 1000.0)
            sample["ok"] = r.status_code < 400
            if not sample["ok"]:
                sample["error"] = f"respondió {r.status_code}"
        except Exception as exc:
            sample["ms"] = round((time.monotonic() - t0) * 1000.0)
            sample["error"] = f"sin respuesta ({type(exc).__name__})"
        self._probes.append(sample)
        self._note_net(sample)
        return sample

    def _note_net(self, sample: dict[str, Any]) -> None:
        if not sample["ok"] and self._net_down_since is None:
            self._net_down_since = sample["ts"]
            self._emit("warn", f"Internet: no se pudo llegar a {PROBE_LABEL} ({sample['error']})")
        elif sample["ok"] and self._net_down_since is not None:
            secs = (sample["ts"] - self._net_down_since) / 1000.0
            self._net_down_since = None
            self._emit("info", f"Internet: volvió la conexión con {PROBE_LABEL} tras {fmt_span(secs)}")
        elif sample["ok"] and sample["ms"] is not None and sample["ms"] > SLOW_MS and self._log.should_log("slow", 1800):
            self._emit("warn", f"Internet lento: {sample['ms']} ms hasta {PROBE_LABEL}")

    async def _probe_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.probe()
            except Exception:
                pass
            await asyncio.sleep(PROBE_S)

    # ------------------------------------------------------------------ the view

    def _battery_now(self) -> tuple[dict | None, str | None]:
        if self._battery is None or time.monotonic() - self._battery[0] > BATTERY_TTL_S:
            info, why = battery_info()
            self._battery = (time.monotonic(), info, why)
        return self._battery[1], self._battery[2]

    async def view(self) -> dict[str, Any]:
        """SystemStatus."""
        started = getattr(self.rt, "started_at", None) or time.time()
        probes = list(self._probes)
        hour_ago = now_ms() - 3600_000
        recent = [p for p in probes if p["ts"] >= hour_ago]
        oks = [p["ms"] for p in recent if p["ok"] and p["ms"] is not None]
        last = probes[-1] if probes else None
        if config.OFFLINE:
            net_state, net_text = "off", "Modo sin conexión (NEXUS_OFFLINE): no comprueba internet"
        elif last is None:
            net_state, net_text = "unknown", "Comprobando la conexión…"
        elif not last["ok"]:
            net_state, net_text = "down", f"Sin conexión con {PROBE_LABEL}: {last['error']}"
        elif last["ms"] is not None and last["ms"] > SLOW_MS:
            net_state, net_text = "slow", f"Lenta: {last['ms']} ms hasta {PROBE_LABEL}"
        else:
            net_state, net_text = "ok", f"Bien: {last['ms']} ms hasta {PROBE_LABEL}"
        battery, battery_note = await asyncio.to_thread(self._battery_now)
        db = getattr(self.rt, "db", None)
        try:
            db_size = int(db.size_bytes()) if db is not None else None
        except Exception:
            db_size = None
        starts = list(reversed(self._starts))[:10]
        return {
            "host": self._host,
            "started_at": int(started * 1000),
            "uptime_s": int(time.time() - started),
            "starts": starts,
            "net": {"state": net_state, "text": net_text, "target": PROBE_LABEL,
                    "last_ms": last["ms"] if last else None,
                    "avg_ms": round(sum(oks) / len(oks)) if oks else None,
                    "checks_1h": len(recent), "fails_1h": sum(1 for p in recent if not p["ok"]),
                    "samples": probes[-30:]},
            "battery": battery,
            "battery_note": battery_note,
            "memory_mb": await asyncio.to_thread(memory_mb),
            "db_size_bytes": db_size,
            "server_time": now_ms(),
        }


def fmt_span(seconds: float) -> str:
    """'45 s' · '12 min' · '2 h 05 min' · '3 d 4 h'."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"
