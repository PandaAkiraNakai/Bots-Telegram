#!/usr/bin/env python3
"""
bot-comandos-torre — control y monitoreo de la torre desde Telegram.

Funciones:
  · Reportes pull: estado (sistema/disco/red/temps/GPU), procesos top,
    servicios (fallidos / clave / start-stop-restart), poder.
  · Alertas push: umbrales con histéresis y cooldown sobre CPU%, RAM%,
    disco%, temp CPU, temp/uso GPU, load por core. Snapshot de top
    procesos adjunto. Botón "silenciar 1 h" en la alerta.
  · Eventos push: servicios que pasan a failed, retorno de suspend,
    nuevos logins, boot fresco, alertas SMART.
  · Histórico: SQLite cada minuto + render PNG de tendencia con matplotlib.
  · Multihost: estado de vpspriv / vpsgames vía SSH.
  · Auditoría: JSONL append-only.
  · Acciones de poder + kill de procesos + control de servicios, todo con
    confirmación 2-pasos.

Stdlib solamente para core. matplotlib y SSH son opcionales.
"""

import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:
    print("Python 3.11+ required (tomllib missing)", file=sys.stderr)
    sys.exit(2)


CONFIG_PATH = os.environ.get("BCT_CONFIG", "/etc/bot-comandos-torre/config.toml")


# ─── Config ──────────────────────────────────────────────────────────────────

DEFAULT_ALERTS = {
    "cpu_pct":         {"hi": 90.0, "lo": 75.0, "cooldown_s": 600},
    "ram_pct":         {"hi": 90.0, "lo": 80.0, "cooldown_s": 600},
    "disk_pct":        {"hi": 90.0, "lo": 80.0, "cooldown_s": 3600},
    "cpu_temp_c":      {"hi": 85.0, "lo": 75.0, "cooldown_s": 600},
    "gpu_temp_c":      {"hi": 85.0, "lo": 75.0, "cooldown_s": 600},
    "gpu_pct":         {"hi": 95.0, "lo": 80.0, "cooldown_s": 600},
    "load1_per_core":  {"hi": 2.0,  "lo": 1.5,  "cooldown_s": 600},
}


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for key in ("bot_token", "chat_id"):
        if key not in cfg:
            print(f"config missing required key: {key}", file=sys.stderr)
            sys.exit(2)
    cfg.setdefault("poll_timeout_s", 25)
    cfg.setdefault("monitor", {})
    cfg["monitor"].setdefault("enabled", True)
    cfg["monitor"].setdefault("interval_s", 60)
    cfg.setdefault("alerts", {})
    for k, v in DEFAULT_ALERTS.items():
        cfg["alerts"].setdefault(k, {})
        for sk, sv in v.items():
            cfg["alerts"][k].setdefault(sk, sv)
    cfg.setdefault("history", {})
    cfg["history"].setdefault("db_path", "/var/lib/bot-comandos-torre/metrics.db")
    cfg["history"].setdefault("retain_days", 14)
    cfg.setdefault("audit", {})
    cfg["audit"].setdefault("log_path", "/var/log/bot-comandos-torre/audit.log")
    cfg.setdefault("network", {})
    cfg["network"].setdefault("ping_hosts", ["1.1.1.1"])
    cfg.setdefault("hosts", {})
    cfg.setdefault("updates", {})
    cfg["updates"].setdefault("command", ["checkupdates"])
    cfg.setdefault("sudo", {})
    cfg["sudo"].setdefault("enabled", False)
    cfg.setdefault("smart", {})
    cfg["smart"].setdefault("devices", [])
    cfg.setdefault("services", {})
    cfg["services"].setdefault("watch", [
        "sshd", "NetworkManager", "systemd-resolved",
        "sudo-telegram", "claude-telegram", "bot-comandos-torre",
        "docker",
    ])
    cfg["services"].setdefault("manageable", [
        "sshd", "NetworkManager",
        "sudo-telegram", "claude-telegram",
        "docker",
    ])
    cfg.setdefault("ssh", {})
    cfg["ssh"].setdefault(
        "known_hosts", "/var/lib/bot-comandos-torre/known_hosts"
    )
    cfg.setdefault("apps", {})
    cfg.setdefault("mpris", {})
    cfg["mpris"].setdefault("ignore_players", [])
    cfg.setdefault("red", {})
    cfg["red"].setdefault("wake", {})
    cfg.setdefault("vps", {})
    cfg["vps"].setdefault("hosts", {})
    cfg.setdefault("steam", {})
    cfg["steam"].setdefault("use_gamescope", True)
    cfg["steam"].setdefault("exclude_appids", [
        "228980", "1070560", "1391110", "1493710", "1628350", "4183110",
    ])
    cfg.setdefault("obsidian", {})
    cfg["obsidian"].setdefault("inbox_path", "")
    # Auto-borrado del chat: borra los mensajes que el bot mandó tras N
    # segundos. 0 desactiva. El reaper chequea cada
    # `chat_auto_delete_interval_s`.
    cfg.setdefault("chat_auto_delete_s", 1800)
    cfg.setdefault("chat_auto_delete_interval_s", 60)
    return cfg


# ─── Telegram ────────────────────────────────────────────────────────────────

class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: int):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = int(chat_id)
        self._send_lock = threading.Lock()
        # Reaper se setea desde main() después de construir; si queda en
        # None, send() no rastrea y nada se borra.
        self.reaper: "MessageReaper | None" = None

    def _post(self, method: str, payload: dict, timeout: int = 10) -> dict:
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(f"{self.base}/{method}", data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(f"network error: {e}") from e
        if not body.get("ok"):
            raise TelegramError(f"api error: {body}")
        return body["result"]

    def _post_multipart(self, method: str, fields: dict, files: dict) -> dict:
        boundary = f"----bct{int(time.time()*1000)}"
        body = bytearray()
        for k, v in fields.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            body += str(v).encode() + b"\r\n"
        for k, (filename, content, ctype) in files.items():
            body += f"--{boundary}\r\n".encode()
            body += (
                f'Content-Disposition: form-data; name="{k}"; '
                f'filename="{filename}"\r\n'
            ).encode()
            body += f"Content-Type: {ctype}\r\n\r\n".encode()
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{self.base}/{method}", data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(f"network error: {e}") from e
        if not resp_body.get("ok"):
            raise TelegramError(f"api error: {resp_body}")
        return resp_body["result"]

    def send(self, text: str, kb: list | None = None) -> int:
        with self._send_lock:
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
            if kb is not None:
                payload["reply_markup"] = json.dumps({"inline_keyboard": kb})
            mid = self._post("sendMessage", payload)["message_id"]
        if self.reaper is not None:
            self.reaper.track(mid)
        return mid

    def delete_message(self, message_id: int) -> None:
        self._post(
            "deleteMessage",
            {"chat_id": self.chat_id, "message_id": int(message_id)},
        )

    def edit(self, message_id: int, text: str, kb: list | None = None) -> None:
        with self._send_lock:
            payload = {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if kb is not None:
                payload["reply_markup"] = json.dumps({"inline_keyboard": kb})
            try:
                self._post("editMessageText", payload)
            except TelegramError as e:
                if "message is not modified" not in str(e):
                    raise

    def edit_kb(self, message_id: int, kb: list) -> None:
        with self._send_lock:
            payload = {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "reply_markup": json.dumps({"inline_keyboard": kb}),
            }
            try:
                self._post("editMessageReplyMarkup", payload)
            except TelegramError:
                pass

    def answer(self, callback_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            self._post("answerCallbackQuery", payload)
        except TelegramError:
            pass

    def send_photo(self, png: bytes, caption: str = "") -> int:
        with self._send_lock:
            fields = {"chat_id": self.chat_id, "parse_mode": "HTML"}
            if caption:
                fields["caption"] = caption
            files = {"photo": ("trend.png", png, "image/png")}
            mid = self._post_multipart("sendPhoto", fields, files)["message_id"]
        if self.reaper is not None:
            self.reaper.track(mid)
        return mid

    def set_commands(self, commands: list) -> None:
        payload = {"commands": json.dumps(commands)}
        try:
            self._post("setMyCommands", payload)
        except TelegramError as e:
            print(f"setMyCommands failed: {e}", file=sys.stderr)

    def set_menu_button(self) -> None:
        payload = {
            "chat_id": self.chat_id,
            "menu_button": json.dumps({"type": "commands"}),
        }
        try:
            self._post("setChatMenuButton", payload)
        except TelegramError as e:
            print(f"setChatMenuButton failed: {e}", file=sys.stderr)

    def updates(self, offset: int, timeout: int) -> list:
        params = urllib.parse.urlencode({
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        })
        url = f"{self.base}/getUpdates?{params}"
        try:
            with urllib.request.urlopen(url, timeout=timeout + 10) as resp:
                body = json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(f"network error: {e}") from e
        if not body.get("ok"):
            raise TelegramError(f"api error: {body}")
        return body["result"]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run(cmd: list[str], timeout: int = 10, env: dict | None = None) -> str:
    if env is None:
        env = {**os.environ, "LC_ALL": "C"}
    try:
        return subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, timeout=timeout, text=True, env=env,
        )
    except subprocess.CalledProcessError as e:
        return e.output or f"<exit {e.returncode}>"
    except subprocess.TimeoutExpired:
        return "<timeout>"
    except FileNotFoundError:
        return f"<no encontrado: {cmd[0]}>"


def fmt_bytes(n: float) -> str:
    units = ["B", "K", "M", "G", "T"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f}{units[i]}"


def fmt_uptime(secs: float) -> str:
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Audit log (append-only JSONL) ───────────────────────────────────────────

class Audit:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def log(self, event: str, **fields) -> None:
        entry = {"ts": now_iso(), "event": event, **fields}
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as e:
                print(f"audit: write failed: {e}", file=sys.stderr)


# ─── Reaper de mensajes (auto-borrado del chat) ──────────────────────────────

class MessageReaper:
    """Borra los mensajes que el bot mandó tras `delete_after_s` segundos.

    Trackea cada message_id devuelto por send/send_photo en una deque
    junto con su timestamp. Un thread chequea cada `interval_s` y borra
    los vencidos. Borra de a uno (Telegram no tiene bulk-delete) y
    absorbe errores — un mensaje ya borrado o >48h no debe matar el
    thread.
    """

    def __init__(self, tg: "Telegram", delete_after_s: int, interval_s: int, audit: "Audit"):
        self.tg = tg
        self.delete_after_s = int(delete_after_s)
        self.interval_s = max(int(interval_s), 5)
        self.audit = audit
        self._lock = threading.Lock()
        self._items: deque[tuple[int, float]] = deque()

    @property
    def enabled(self) -> bool:
        return self.delete_after_s > 0

    def track(self, message_id: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._items.append((int(message_id), time.time()))

    def _drain_due(self) -> list[int]:
        now = time.time()
        due: list[int] = []
        with self._lock:
            while self._items and now - self._items[0][1] >= self.delete_after_s:
                due.append(self._items.popleft()[0])
        return due

    def reap_once(self) -> int:
        ok = 0
        for mid in self._drain_due():
            try:
                self.tg.delete_message(mid)
                ok += 1
            except TelegramError:
                pass
        if ok:
            self.audit.log("chat_reaped", count=ok)
        return ok

    def run(self) -> None:
        while True:
            time.sleep(self.interval_s)
            try:
                self.reap_once()
            except Exception as e:
                print(f"reaper: {e}", file=sys.stderr)


# ─── Métricas ────────────────────────────────────────────────────────────────

class Metrics:
    """Estado entre snapshots para calcular deltas (CPU%, throughput de red)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cpu_prev: tuple[int, int] | None = None
        self._net_prev: dict[str, tuple[int, int, float]] = {}

    def cpu_pct(self) -> float:
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
        except (OSError, ValueError):
            return 0.0
        with self._lock:
            prev = self._cpu_prev
            self._cpu_prev = (idle, total)
        if prev is None:
            return 0.0
        d_idle = idle - prev[0]
        d_total = total - prev[1]
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, (1 - d_idle / d_total) * 100))

    def cpu_count(self) -> int:
        return os.cpu_count() or 1

    def loadavg(self) -> tuple[float, float, float]:
        try:
            with open("/proc/loadavg") as f:
                a, b, c = f.read().split()[:3]
            return float(a), float(b), float(c)
        except (OSError, ValueError):
            return 0.0, 0.0, 0.0

    def memory(self) -> dict:
        try:
            mi: dict[str, int] = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    mi[k] = int(v.strip().split()[0])
        except (OSError, ValueError, KeyError):
            return {"total_g": 0.0, "used_g": 0.0, "pct": 0.0}
        total_kb = mi.get("MemTotal", 0)
        avail_kb = mi.get("MemAvailable", mi.get("MemFree", 0))
        used_kb = total_kb - avail_kb
        pct = 100 * used_kb / total_kb if total_kb else 0.0
        return {
            "total_g": total_kb / 1024 / 1024,
            "used_g": used_kb / 1024 / 1024,
            "pct": pct,
        }

    def disk(self) -> list[dict]:
        out = run([
            "df", "-k", "--output=source,size,used,avail,pcent,target",
            "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay", "-x", "squashfs",
            "-x", "efivarfs", "-x", "fuse.portal", "-x", "nsfs",
        ])
        result = []
        for line in out.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                size_k = int(parts[1])
                used_k = int(parts[2])
            except ValueError:
                continue
            pct_str = parts[4].rstrip("%")
            try:
                pct = float(pct_str)
            except ValueError:
                pct = 0.0
            result.append({
                "source": parts[0],
                "mount": parts[5],
                "size_g": size_k / 1024 / 1024,
                "used_g": used_k / 1024 / 1024,
                "pct": pct,
            })
        return result

    def disk_max_pct(self) -> tuple[str, float]:
        rows = self.disk()
        if not rows:
            return "", 0.0
        worst = max(rows, key=lambda r: r["pct"])
        return worst["mount"], worst["pct"]

    def temps(self) -> list[dict]:
        out = run(["sensors", "-A", "-j"], timeout=5)
        if not out.startswith("<"):
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                data = None
            if data:
                rows = []
                for chip, content in data.items():
                    if not isinstance(content, dict):
                        continue
                    for label, sensor in content.items():
                        if not isinstance(sensor, dict):
                            continue
                        for k, v in sensor.items():
                            if k.endswith("_input") and isinstance(v, (int, float)):
                                rows.append({
                                    "chip": chip,
                                    "label": label,
                                    "c": float(v),
                                })
                                break
                if rows:
                    return rows
        rows = []
        for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            try:
                ttype = (z / "type").read_text().strip()
                temp = int((z / "temp").read_text().strip()) / 1000
                rows.append({"chip": "thermal", "label": ttype, "c": temp})
            except (OSError, ValueError):
                continue
        return rows

    def cpu_temp_max(self) -> float:
        rows = self.temps()
        candidates = []
        for r in rows:
            label = (r["label"] or "").lower()
            chip = (r["chip"] or "").lower()
            if any(k in label for k in (
                "tctl", "tdie", "package id", "core ", "core_", "cpu",
            )) or any(k in chip for k in ("k10temp", "coretemp", "zenpower")):
                candidates.append(r["c"])
        return max(candidates) if candidates else 0.0

    def gpus(self) -> list[dict]:
        result: list[dict] = []
        for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
            dev = card / "device"
            try:
                vendor = (dev / "vendor").read_text().strip()
            except OSError:
                continue
            if vendor not in ("0x1002", "0x10de", "0x8086"):
                continue
            entry = {
                "name": f"{card.name}",
                "vendor": {"0x1002": "AMD", "0x10de": "NVIDIA", "0x8086": "Intel"}[vendor],
                "pct": None, "mem_pct": None,
                "mem_used_g": None, "mem_total_g": None,
                "temp_c": None, "power_w": None, "fan_rpm": None,
            }
            busy = dev / "gpu_busy_percent"
            if busy.exists():
                try:
                    entry["pct"] = float(busy.read_text().strip())
                except (OSError, ValueError):
                    pass
            mem_used = dev / "mem_info_vram_used"
            mem_total = dev / "mem_info_vram_total"
            if mem_used.exists() and mem_total.exists():
                try:
                    u = int(mem_used.read_text().strip())
                    t = int(mem_total.read_text().strip())
                    entry["mem_used_g"] = u / 1024**3
                    entry["mem_total_g"] = t / 1024**3
                    if t:
                        entry["mem_pct"] = 100 * u / t
                except (OSError, ValueError):
                    pass
            for hwmon in (dev / "hwmon").glob("hwmon*") if (dev / "hwmon").exists() else []:
                for f in hwmon.glob("temp*_input"):
                    try:
                        c = int(f.read_text().strip()) / 1000
                        if entry["temp_c"] is None or c > entry["temp_c"]:
                            entry["temp_c"] = c
                    except (OSError, ValueError):
                        continue
                p = hwmon / "power1_average"
                if p.exists():
                    try:
                        entry["power_w"] = int(p.read_text().strip()) / 1_000_000
                    except (OSError, ValueError):
                        pass
                fan = hwmon / "fan1_input"
                if fan.exists():
                    try:
                        entry["fan_rpm"] = int(fan.read_text().strip())
                    except (OSError, ValueError):
                        pass
            result.append(entry)

        if shutil.which("nvidia-smi") and not any(g["vendor"] == "NVIDIA" and g["pct"] is not None for g in result):
            out = run([
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ], timeout=5)
            if not out.startswith("<"):
                for line in out.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 6:
                        continue
                    try:
                        result.append({
                            "name": parts[0], "vendor": "NVIDIA",
                            "pct": float(parts[1]),
                            "mem_used_g": float(parts[2]) / 1024,
                            "mem_total_g": float(parts[3]) / 1024,
                            "mem_pct": 100 * float(parts[2]) / float(parts[3]) if float(parts[3]) else None,
                            "temp_c": float(parts[4]),
                            "power_w": float(parts[5]),
                            "fan_rpm": None,
                        })
                    except ValueError:
                        continue
        return result

    def gpu_max_temp(self) -> float:
        return max((g["temp_c"] for g in self.gpus() if g["temp_c"]), default=0.0)

    def gpu_max_pct(self) -> float:
        return max((g["pct"] for g in self.gpus() if g["pct"]), default=0.0)

    def net_throughput(self) -> dict[str, dict]:
        try:
            with open("/proc/net/dev") as f:
                lines = f.readlines()[2:]
        except OSError:
            return {}
        now = time.monotonic()
        result = {}
        with self._lock:
            for line in lines:
                if ":" not in line:
                    continue
                name, _, rest = line.partition(":")
                name = name.strip()
                if name == "lo":
                    continue
                cols = rest.split()
                rx = int(cols[0])
                tx = int(cols[8])
                prev = self._net_prev.get(name)
                self._net_prev[name] = (rx, tx, now)
                if prev:
                    dt = now - prev[2]
                    if dt > 0:
                        result[name] = {
                            "rx_bps": (rx - prev[0]) / dt,
                            "tx_bps": (tx - prev[1]) / dt,
                            "rx_total": rx, "tx_total": tx,
                        }
                else:
                    result[name] = {
                        "rx_bps": 0.0, "tx_bps": 0.0,
                        "rx_total": rx, "tx_total": tx,
                    }
        return result


# ─── SQLite (histórico) ──────────────────────────────────────────────────────

class History:
    def __init__(self, path: str, retain_days: int):
        self.path = path
        self.retain_days = retain_days
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                ts INTEGER PRIMARY KEY,
                cpu_pct REAL,
                ram_pct REAL,
                gpu_pct REAL,
                cpu_temp REAL,
                gpu_temp REAL,
                load1 REAL,
                disk_pct REAL
            )
        """)
        self._conn.commit()

    def insert(self, ts: int, snap: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?)",
                (
                    ts,
                    snap.get("cpu_pct"),
                    snap.get("ram_pct"),
                    snap.get("gpu_pct"),
                    snap.get("cpu_temp"),
                    snap.get("gpu_temp"),
                    snap.get("load1"),
                    snap.get("disk_pct"),
                ),
            )
            self._conn.commit()

    def prune(self) -> None:
        cutoff = int(time.time()) - self.retain_days * 86400
        with self._lock:
            self._conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
            self._conn.commit()

    def fetch(self, since_s: int) -> list[tuple]:
        cutoff = int(time.time()) - since_s
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts,cpu_pct,ram_pct,gpu_pct,cpu_temp,gpu_temp,load1,disk_pct "
                "FROM metrics WHERE ts >= ? ORDER BY ts", (cutoff,),
            )
            return cur.fetchall()


def render_trend_png(rows: list[tuple], window_label: str) -> bytes | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.dates import DateFormatter
    except ImportError:
        return None
    if not rows:
        return None
    ts = [datetime.fromtimestamp(r[0]) for r in rows]
    series = {
        "CPU %":  [r[1] for r in rows],
        "RAM %":  [r[2] for r in rows],
        "GPU %":  [r[3] for r in rows],
        "T CPU":  [r[4] for r in rows],
        "T GPU":  [r[5] for r in rows],
        "Disco %": [r[7] for r in rows],
    }
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    for label in ("CPU %", "RAM %", "GPU %", "Disco %"):
        ax1.plot(ts, series[label], label=label, linewidth=1.2)
    ax1.set_ylabel("%")
    ax1.set_ylim(0, 100)
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)
    ax1.set_title(f"Tendencia — {window_label}")
    for label in ("T CPU", "T GPU"):
        ax2.plot(ts, series[label], label=label, linewidth=1.2)
    ax2.set_ylabel("°C")
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


# ─── Multihost SSH ───────────────────────────────────────────────────────────

class SSHRunner:
    def __init__(self, known_hosts: str):
        self.known_hosts = known_hosts
        Path(known_hosts).parent.mkdir(parents=True, exist_ok=True)
        Path(known_hosts).touch(exist_ok=True)

    def run(self, alias: str, cmd: str, timeout: int = 8) -> str:
        full = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=2",
            "-o", "ServerAliveCountMax=2",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "StrictHostKeyChecking=accept-new",
            alias, cmd,
        ]
        return run(full, timeout=timeout)


# ─── Alertas ─────────────────────────────────────────────────────────────────

class Alerts:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._state: dict[str, dict] = {
            k: {"armed": True, "last_fired": 0.0, "snoozed_until": 0.0}
            for k in DEFAULT_ALERTS
        }
        self._lock = threading.Lock()

    def snooze(self, key: str, seconds: int) -> None:
        with self._lock:
            if key in self._state:
                self._state[key]["snoozed_until"] = time.time() + seconds

    def evaluate(self, key: str, value: float) -> bool:
        if key not in self._state:
            return False
        thresh = self.cfg["alerts"].get(key, {})
        hi = float(thresh.get("hi", 0))
        lo = float(thresh.get("lo", 0))
        cooldown = float(thresh.get("cooldown_s", 600))
        now = time.time()
        with self._lock:
            st = self._state[key]
            if now < st["snoozed_until"]:
                if value <= lo:
                    st["armed"] = True
                return False
            if st["armed"] and value >= hi:
                if now - st["last_fired"] >= cooldown:
                    st["last_fired"] = now
                    st["armed"] = False
                    return True
            elif not st["armed"] and value <= lo:
                st["armed"] = True
        return False


# ─── Servicios y procesos ────────────────────────────────────────────────────

def list_failed_services() -> list[str]:
    out = run([
        "systemctl", "list-units", "--state=failed",
        "--no-legend", "--no-pager", "--plain",
    ])
    if out.startswith("<"):
        return []
    units = []
    for line in out.strip().splitlines():
        parts = line.split()
        if parts:
            units.append(parts[0])
    return units


def list_sessions() -> set[str]:
    out = run(["loginctl", "list-sessions", "--no-legend", "--no-pager"])
    if out.startswith("<"):
        return set()
    ids = set()
    for line in out.strip().splitlines():
        parts = line.split()
        if parts:
            ids.add(parts[0])
    return ids


def top_processes(by: str, n: int = 10) -> list[list[str]]:
    sort_key = "-pcpu" if by == "cpu" else "-pmem"
    out = run([
        "ps", "-eo", "pid,user:12,pcpu,pmem,comm",
        "--sort", sort_key, "--no-headers",
    ])
    rows = []
    for line in out.strip().splitlines()[:n]:
        parts = line.split(None, 4)
        if len(parts) == 5:
            rows.append(parts)
    return rows


# ─── Niri (compositor) ───────────────────────────────────────────────────────

def niri_socket_env() -> dict:
    """env para invocar `niri msg`. niri abre /run/user/<uid>/niri.<...>.sock
    y lo expone como NIRI_SOCKET dentro de la sesión, pero el bot corre fuera
    de esa env, así que descubrimos el socket por filesystem (el más reciente)."""
    uid = os.getuid()
    candidates = sorted(
        Path(f"/run/user/{uid}").glob("niri.*.sock"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    env = {**os.environ, "LC_ALL": "C"}
    if candidates:
        env["NIRI_SOCKET"] = str(candidates[0])
    return env


def _niri_run(args: list[str], timeout: int = 5) -> tuple[int, str, str]:
    env = niri_socket_env()
    if "NIRI_SOCKET" not in env:
        return -1, "", "no encontré socket de niri (¿está corriendo?)"
    try:
        proc = subprocess.run(
            ["niri", "msg", *args],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return -1, "", "timeout consultando niri"
    except FileNotFoundError:
        return -1, "", "niri no encontrado"
    return proc.returncode, proc.stdout or "", (proc.stderr or "").strip()


def niri_outputs() -> tuple[list[dict], str | None]:
    """Devuelve (outputs, error). outputs es lista de dicts con
    {name, label, on}. on=True si el output está activo en el layout."""
    rc, stdout, stderr = _niri_run(["-j", "outputs"])
    if rc != 0:
        return [], (stderr or stdout or f"exit {rc}")[:300]
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return [], f"JSON inválido: {e}"
    out = []
    for name, info in sorted(data.items()):
        if not isinstance(info, dict):
            continue
        make = (info.get("make") or "").strip()
        model = (info.get("model") or "").strip()
        label = " ".join(p for p in (make, model) if p) or name
        on = info.get("logical") is not None
        out.append({"name": name, "label": label, "on": on})
    return out, None


def niri_set_output(name: str, on: bool) -> str:
    rc, stdout, stderr = _niri_run(["output", name, "on" if on else "off"])
    if rc == 0:
        return "ok"
    return (stderr or stdout or f"exit {rc}")[:300]


def niri_dpms(on: bool) -> str:
    action = "power-on-monitors" if on else "power-off-monitors"
    rc, stdout, stderr = _niri_run(["action", action])
    if rc == 0:
        return "ok"
    return (stderr or stdout or f"exit {rc}")[:300]


# ─── Audio (pactl / PipeWire) ────────────────────────────────────────────────

def _pactl_env() -> dict:
    """env para hablar con la sesión pipewire del usuario.
    El bot corre como sergioc pero fuera de la sesión gráfica, así que hay que
    apuntar XDG_RUNTIME_DIR al runtime dir del usuario."""
    uid = os.getuid()
    return {
        **os.environ,
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "LC_ALL": "C.UTF-8",
    }


def _pactl_run(args: list[str], timeout: int = 5) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["pactl", *args],
            capture_output=True, text=True, timeout=timeout, env=_pactl_env(),
        )
    except subprocess.TimeoutExpired:
        return -1, "", "timeout consultando pactl"
    except FileNotFoundError:
        return -1, "", "pactl no encontrado"
    return proc.returncode, proc.stdout or "", (proc.stderr or "").strip()


def audio_sinks() -> tuple[list[dict], str | None]:
    """Devuelve (sinks, error). sinks = lista de dicts {name, label, icon, kind}."""
    rc, stdout, stderr = _pactl_run(["-f", "json", "list", "sinks"])
    if rc != 0:
        return [], (stderr or stdout or f"exit {rc}")[:300]
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return [], f"JSON inválido: {e}"
    out = []
    for s in data:
        name = s.get("name") or ""
        if not name:
            continue
        props = s.get("properties") or {}
        prof = (props.get("device.profile.name") or "").lower()
        if "hdmi" in prof or "hdmi" in name.lower():
            kind, icon = "hdmi", "📺"
        elif "analog" in prof or "analog" in name.lower():
            kind, icon = "analog", "🎧"
        elif "bluez" in name.lower() or "bluetooth" in prof:
            kind, icon = "bt", "🔵"
        elif "usb" in (props.get("device.bus") or "").lower():
            kind, icon = "usb", "🎚"
        else:
            kind, icon = "other", "🔊"
        label = (
            (props.get("node.nick") or "").strip()
            or (props.get("alsa.name") or "").strip()
            or (props.get("device.description") or "").strip()
            or name
        )
        out.append({"name": name, "label": label, "icon": icon, "kind": kind})
    return out, None


def audio_default_sink() -> tuple[str, str | None]:
    rc, stdout, stderr = _pactl_run(["get-default-sink"])
    if rc != 0:
        return "", (stderr or stdout or f"exit {rc}")[:300]
    return stdout.strip(), None


def audio_set_sink(sink: str) -> str:
    """Cambia el sink por defecto Y mueve los streams ya en curso, para que lo
    que esté sonando salte al sink nuevo de inmediato."""
    rc, _stdout, stderr = _pactl_run(["set-default-sink", sink])
    if rc != 0:
        return (stderr or f"exit {rc}")[:300]
    rc, stdout, _stderr = _pactl_run(["list", "short", "sink-inputs"])
    if rc == 0:
        for line in stdout.splitlines():
            parts = line.split("\t", 1)
            input_id = parts[0].strip() if parts else ""
            if input_id.isdigit():
                _pactl_run(["move-sink-input", input_id, sink])
    return "ok"


# ─── MPRIS (playerctl) ───────────────────────────────────────────────────────

def _playerctl_env() -> dict:
    uid = os.getuid()
    return {
        **os.environ,
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "LC_ALL": "C.UTF-8",
    }


def _playerctl_run(args: list[str], timeout: int = 5) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["playerctl", *args],
            capture_output=True, text=True, timeout=timeout, env=_playerctl_env(),
        )
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "playerctl no encontrado"
    return proc.returncode, proc.stdout or "", (proc.stderr or "").strip()


def mpris_players(cfg: dict | None = None) -> list[str]:
    rc, stdout, _ = _playerctl_run(["-l"])
    if rc != 0:
        return []
    ignore = set((cfg or {}).get("mpris", {}).get("ignore_players") or [])
    return [p.strip() for p in stdout.splitlines() if p.strip() and p.strip() not in ignore]


def mpris_status(player: str) -> dict:
    args = ["-p", player]
    out: dict = {"status": "?", "title": "", "artist": "", "album": ""}
    rc, stdout, _ = _playerctl_run([*args, "status"])
    if rc == 0:
        out["status"] = stdout.strip()
    for field in ("title", "artist", "album"):
        rc, stdout, _ = _playerctl_run([*args, "metadata", f"xesam:{field}"])
        if rc == 0:
            out[field] = stdout.strip()
    return out


def mpris_action(action: str, player: str) -> str:
    base = ["-p", player]
    if action == "vol+":
        args = [*base, "volume", "0.05+"]
    elif action == "vol-":
        args = [*base, "volume", "0.05-"]
    elif action in ("play-pause", "next", "previous"):
        args = [*base, action]
    elif action.startswith("seek:"):
        # seek:+10  → adelanta 10 s
        # seek:-30  → retrocede 30 s
        # playerctl espera "10+" / "30-" (sufijo, no prefijo).
        delta = action[5:]
        if not delta or delta[0] not in "+-" or not delta[1:].isdigit():
            return f"seek inválido: {delta}"
        sign = delta[0]
        secs = delta[1:]
        args = [*base, "position", f"{secs}{sign}"]
    else:
        return f"acción desconocida: {action}"
    rc, _, stderr = _playerctl_run(args)
    if rc == 0:
        return "ok"
    # Players que no implementan Seek (Spotify clásico, Apple Music web)
    # devuelven algo como "Seek is not supported" o similar. Lo dejamos
    # pasar tal cual para que el chat lo muestre.
    return (stderr[:200] or f"exit {rc}")


# ─── Red (LAN scan + Wake-on-LAN) ────────────────────────────────────────────

def lan_neighbors() -> list[dict]:
    out = run(["ip", "-4", "neigh", "show"], timeout=5)
    rows = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        ip = parts[0]
        dev = parts[parts.index("dev") + 1] if "dev" in parts else "?"
        mac = parts[parts.index("lladdr") + 1] if "lladdr" in parts else ""
        state = parts[-1]
        rows.append({"ip": ip, "mac": mac, "dev": dev, "state": state})
    return rows


def default_gateway() -> str:
    out = run(["ip", "route", "show", "default"], timeout=3).strip().splitlines()
    if not out:
        return ""
    parts = out[0].split()
    return parts[parts.index("via") + 1] if "via" in parts else ""


def wol_send(target: str, cfg: dict) -> str:
    mapping = (cfg.get("red") or {}).get("wake") or {}
    mac = mapping.get(target, target)
    if not re.match(r"^[0-9A-Fa-f]{2}([:.\-][0-9A-Fa-f]{2}){5}$", mac):
        return f"MAC inválida o host no mapeado: {target}"
    out = run(["wol", mac], timeout=5)
    low = out.lower()
    if "waking up" in low or "wake-up" in low or "magic packet" in low:
        return "ok"
    return out.strip()[:200] or "ok"


def lan_scan_pretty() -> str:
    gw = default_gateway()
    nbrs = lan_neighbors()
    lines = [">> NETSCAN", "-" * 40]
    lines.append(f"gateway: {gw or '?'}")
    lines.append(f"vecinos: {len(nbrs)}")
    lines.append("")
    lines.append(f"{'IP':<16}{'MAC':<19}{'IF':<8}STATE")
    for n in nbrs[:30]:
        lines.append(f"{n['ip']:<16}{n['mac']:<19}{n['dev']:<8}{n['state']}")
    if gw:
        lines.append("")
        lines.append(f">> TRACEROUTE {gw}")
        tr = run(["traceroute", "-n", "-q", "1", "-m", "5", gw], timeout=10)
        lines.append(tr.strip())
    return "\n".join(lines)


# ─── VPS bridge (SSH a servers conocidos) ────────────────────────────────────

DEFAULT_VPS_SUMMARY = (
    "echo '== uptime ==' && uptime && "
    "echo '== disk /  ==' && df -hT / | tail -1 && "
    "echo '== mem    ==' && free -h | sed -n '1,2p' && "
    "echo '== docker ==' && (docker ps --format '{{.Names}}\\t{{.Status}}' 2>/dev/null | head -10 || echo 'sin docker')"
)


def vps_status(cfg: dict, alias: str) -> tuple[str, str | None]:
    cfg_vps = (cfg.get("vps") or {}).get("hosts") or {}
    spec = cfg_vps.get(alias)
    if not isinstance(spec, dict):
        return "", f"VPS '{alias}' no configurado"
    ssh_alias = spec.get("ssh_alias") or alias
    cmd = spec.get("summary_cmd") or DEFAULT_VPS_SUMMARY
    out = run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         ssh_alias, "bash", "-lc", cmd],
        timeout=20,
    )
    return out, None


# ─── Steam / Games ───────────────────────────────────────────────────────────

def steam_library_paths() -> list[Path]:
    """Descubre todas las bibliotecas de Steam parseando libraryfolders.vdf.
    Devuelve los directorios `steamapps/` de cada biblioteca (la default y las
    externas — SSD, HDD, etc.). Cae al default si el VDF no existe o no
    parsea."""
    default_steamapps = Path.home() / ".local/share/Steam/steamapps"
    vdf = default_steamapps / "libraryfolders.vdf"
    libs: list[Path] = []
    if vdf.exists():
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'"path"\s*"([^"]+)"', text):
                p = Path(m.group(1)) / "steamapps"
                if p not in libs:
                    libs.append(p)
        except OSError:
            pass
    if not libs and default_steamapps.exists():
        libs.append(default_steamapps)
    return [p for p in libs if p.exists()]


def steam_games(cfg: dict) -> list[dict]:
    exclude = set((cfg.get("steam") or {}).get("exclude_appids") or [])
    games = []
    seen = set()
    for base in steam_library_paths():
        for f in base.glob("appmanifest_*.acf"):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m_id = re.search(r'"appid"\s*"(\d+)"', text)
            m_name = re.search(r'"name"\s*"([^"]+)"', text)
            if not m_id or not m_name:
                continue
            appid = m_id.group(1)
            if appid in exclude or appid in seen:
                continue
            name = m_name.group(1)
            if any(s in name for s in ("Steam Linux Runtime", "Proton", "Steamworks")):
                continue
            seen.add(appid)
            games.append({"appid": appid, "name": name})
    games.sort(key=lambda g: g["name"].lower())
    return games


def steam_launch(appid: str, cfg: dict) -> str:
    use_gamescope = (cfg.get("steam") or {}).get("use_gamescope", True)
    gamescope = Path.home() / ".local/bin/gamescope-auto"
    if use_gamescope and gamescope.exists():
        cmd = [str(gamescope), f"steam://rungameid/{appid}"]
    else:
        cmd = ["steam", f"steam://rungameid/{appid}"]
    uid = os.getuid()
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{uid}", "LC_ALL": "C"}
    full = ["systemd-run", "--user", "--collect", "--quiet", "--", *cmd]
    try:
        proc = subprocess.run(
            full, capture_output=True, text=True, timeout=10, env=env,
        )
    except subprocess.TimeoutExpired:
        return "timeout esperando a systemd-run"
    except FileNotFoundError:
        return "systemd-run no encontrado"
    if proc.returncode == 0:
        return "ok"
    return ((proc.stderr or proc.stdout or "").strip()
            or f"exit {proc.returncode}")


# ─── Obsidian (nota rápida al inbox) ─────────────────────────────────────────

def note_append(text: str, cfg: dict) -> str:
    obs = cfg.get("obsidian") or {}
    inbox = (obs.get("inbox_path") or "").strip()
    if not inbox:
        return "obsidian.inbox_path no configurado en config.toml"
    p = Path(inbox)
    if not p.parent.exists():
        return f"vault no disponible (¿HDD montado?): {p.parent}"
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- **{ts}** — {text.strip()}\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(entry)
        return "ok"
    except OSError as e:
        return str(e)


# ─── Menús ───────────────────────────────────────────────────────────────────

MAIN_KB = [
    [{"text": "⚡ Estado", "callback_data": "estado"},
     {"text": "💀 Procesos", "callback_data": "procesos"}],
    [{"text": "⚙ Servicios", "callback_data": "servicios"},
     {"text": "💾 Updates", "callback_data": "updates"}],
    [{"text": "🔮 Tendencia", "callback_data": "trend"},
     {"text": "🪄 Apps", "callback_data": "apps"}],
    [{"text": "🎚 Media", "callback_data": "media"},
     {"text": "🕹 Juegos", "callback_data": "games"}],
    [{"text": "📡 Red", "callback_data": "red"},
     {"text": "🛸 VPS", "callback_data": "vps"}],
    [{"text": "🖥 Pantallas", "callback_data": "pantallas"},
     {"text": "🎧 Audio", "callback_data": "audio"},
     {"text": "☢ Poder", "callback_data": "poder"}],
]

ESTADO_KB = [
    [{"text": "Sistema", "callback_data": "estado:sistema"},
     {"text": "Disco", "callback_data": "estado:disco"}],
    [{"text": "Red", "callback_data": "estado:red"},
     {"text": "Temperaturas", "callback_data": "estado:temp"}],
    [{"text": "GPU", "callback_data": "estado:gpu"},
     {"text": "SMART", "callback_data": "estado:smart"}],
    [{"text": "← Volver", "callback_data": "main"}],
]

PROCESOS_KB = [
    [{"text": "Top CPU", "callback_data": "procesos:cpu"},
     {"text": "Top RAM", "callback_data": "procesos:ram"}],
    [{"text": "← Volver", "callback_data": "main"}],
]

PODER_KB = [
    [{"text": "🔴 Apagar", "callback_data": "poder:off"},
     {"text": "🔁 Reiniciar", "callback_data": "poder:reboot"}],
    [{"text": "💤 Suspender", "callback_data": "poder:suspend"},
     {"text": "🔒 Bloquear", "callback_data": "poder:lock"}],
    [{"text": "← Volver", "callback_data": "main"}],
]

TREND_KB = [
    [{"text": "1 h",  "callback_data": "trend:3600"},
     {"text": "6 h",  "callback_data": "trend:21600"},
     {"text": "24 h", "callback_data": "trend:86400"}],
    [{"text": "← Volver", "callback_data": "main"}],
]

POWER_LABELS = {
    "off": "apagar", "reboot": "reiniciar",
    "suspend": "suspender", "lock": "bloquear sesión",
}


def confirm_kb(token: str, cancel_to: str = "main") -> list:
    return [[
        {"text": "✅ Sí", "callback_data": f"confirm:{token}"},
        {"text": "❌ No", "callback_data": cancel_to},
    ]]


def back_kb(parent: str) -> list:
    return [[{"text": "← Volver", "callback_data": parent}]]


def servicios_kb(cfg: dict) -> list:
    return [
        [{"text": "Fallidos", "callback_data": "servicios:failed"},
         {"text": "Activos clave", "callback_data": "servicios:clave"}],
        [{"text": "Controlar", "callback_data": "servicios:manage"}],
        [{"text": "← Volver", "callback_data": "main"}],
    ]


def manage_services_kb(cfg: dict) -> list:
    rows = []
    line = []
    for svc in cfg["services"]["manageable"]:
        line.append({"text": svc, "callback_data": f"svcmenu:{svc}"})
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([{"text": "← Volver", "callback_data": "servicios"}])
    return rows


def svc_actions_kb(svc: str) -> list:
    return [
        [{"text": "▶ Start", "callback_data": f"svcact:{svc}:start"},
         {"text": "⏹ Stop", "callback_data": f"svcact:{svc}:stop"}],
        [{"text": "🔄 Restart", "callback_data": f"svcact:{svc}:restart"},
         {"text": "ℹ Status", "callback_data": f"svcact:{svc}:status"}],
        [{"text": "← Volver", "callback_data": "servicios:manage"}],
    ]


def hosts_kb(cfg: dict) -> list:
    rows = []
    for name in cfg["hosts"]:
        rows.append([{"text": name, "callback_data": f"host:{name}"}])
    if not rows:
        rows.append([{"text": "(sin hosts configurados)", "callback_data": "main"}])
    rows.append([{"text": "← Volver", "callback_data": "main"}])
    return rows


def apps_kb(cfg: dict) -> list:
    rows = []
    line = []
    for name, spec in cfg["apps"].items():
        if not isinstance(spec, dict):
            continue
        label = spec.get("label") or name
        line.append({"text": label, "callback_data": f"app:{name}"})
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    if not rows:
        rows.append([{"text": "(sin apps configuradas)", "callback_data": "main"}])
    rows.append([{"text": "← Volver", "callback_data": "main"}])
    return rows


def audio_kb(sinks: list[dict], default: str) -> list:
    rows = []
    for s in sinks:
        active = (s["name"] == default)
        label = f"{s['icon']} {s['label']}" + (" ✅" if active else "")
        cb = "noop" if active else f"audio:set:{s['name']}"
        # Telegram limita callback_data a 64 bytes; si un sink tiene nombre
        # raro y largo, lo dejamos como noop antes que romper el botón.
        if len(cb.encode("utf-8")) > 64:
            cb = "noop"
        rows.append([{"text": label, "callback_data": cb}])
    rows.append([
        {"text": "🔄 Refrescar", "callback_data": "audio"},
        {"text": "← Volver", "callback_data": "main"},
    ])
    return rows


def pantallas_kb(outputs: list[dict]) -> list:
    rows = []
    for o in outputs:
        if o["on"]:
            label = f"⭕ Apagar {o['name']}"
            cb = f"pant:off:{o['name']}"
        else:
            label = f"✅ Encender {o['name']}"
            cb = f"pant:on:{o['name']}"
        rows.append([{"text": label, "callback_data": cb}])
    rows.append([
        {"text": "🌑 DPMS off", "callback_data": "pant:dpms_off"},
        {"text": "🌕 DPMS on", "callback_data": "pant:dpms_on"},
    ])
    rows.append([
        {"text": "🔄 Refrescar", "callback_data": "pantallas"},
        {"text": "← Volver", "callback_data": "main"},
    ])
    return rows


def media_kb(players: list[str], selected: str, status: str) -> list:
    play_label = "⏸ Pausa" if status == "Playing" else "▶ Play"
    rows = [
        [
            {"text": "⏮ Prev", "callback_data": "media:prev"},
            {"text": play_label, "callback_data": "media:toggle"},
            {"text": "⏭ Next", "callback_data": "media:next"},
        ],
        [
            {"text": "⏪ −30", "callback_data": "media:seek:-30"},
            {"text": "⏪ −10", "callback_data": "media:seek:-10"},
            {"text": "⏩ +10", "callback_data": "media:seek:+10"},
            {"text": "⏩ +30", "callback_data": "media:seek:+30"},
        ],
        [
            {"text": "🔉 Vol−", "callback_data": "media:vol-"},
            {"text": "🔊 Vol+", "callback_data": "media:vol+"},
        ],
    ]
    if len(players) > 1:
        line = []
        for i, p in enumerate(players[:6]):
            mark = "✓ " if p == selected else ""
            short = p.split(".", 1)[0][:18]
            line.append({"text": f"{mark}{short}", "callback_data": f"media:pick:{i}"})
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
    rows.append([
        {"text": "🔄 Refrescar", "callback_data": "media"},
        {"text": "← Volver", "callback_data": "main"},
    ])
    return rows


def red_kb() -> list:
    return [
        [{"text": "📡 Vecinos LAN", "callback_data": "red:lan"},
         {"text": "🔭 Scan", "callback_data": "red:scan"}],
        [{"text": "⚡ Wake-on-LAN", "callback_data": "red:wake"}],
        [{"text": "← Volver", "callback_data": "main"}],
    ]


def vps_kb(cfg: dict) -> list:
    rows = []
    cfg_vps = (cfg.get("vps") or {}).get("hosts") or {}
    for name in cfg_vps:
        rows.append([{"text": f"🛸 {name}", "callback_data": f"vps:show:{name}"}])
    if not rows:
        rows.append([{"text": "(sin VPS configurados)", "callback_data": "main"}])
    rows.append([{"text": "← Volver", "callback_data": "main"}])
    return rows


def games_kb(games: list[dict]) -> list:
    rows = []
    for g in games[:20]:
        cb = f"game:launch:{g['appid']}"
        if len(cb.encode("utf-8")) > 64:
            continue
        rows.append([{"text": f"▶ {g['name'][:40]}", "callback_data": cb}])
    rows.append([
        {"text": "🔄 Refrescar", "callback_data": "games"},
        {"text": "← Volver", "callback_data": "main"},
    ])
    return rows


def kill_kb(by: str, rows: list[list[str]]) -> list:
    btns = []
    line = []
    for r in rows[:10]:
        pid = r[0]
        line.append({"text": f"☠ {pid}", "callback_data": f"killask:{pid}"})
        if len(line) == 2:
            btns.append(line)
            line = []
    if line:
        btns.append(line)
    btns.append([{"text": "← Volver", "callback_data": f"procesos:{by}"}])
    return btns


# ─── Reportes ────────────────────────────────────────────────────────────────

def report_main(_):
    return "<b>Bot Comandos Torre</b>\nElige una categoría:", MAIN_KB


def report_estado(_):
    return "<b>⚡ Estado</b>\nElige qué ver:", ESTADO_KB


def report_procesos(_):
    return "<b>💀 Procesos</b>\nElige qué ver:", PROCESOS_KB


def report_servicios(ctx):
    return "<b>⚙ Servicios</b>\nElige qué ver:", servicios_kb(ctx.cfg)


def report_servicios_manage(ctx):
    return (
        "<b>⚙ Controlar servicios</b>\nElige cuál:",
        manage_services_kb(ctx.cfg),
    )


def report_poder(_):
    return "<b>☢ Poder</b>\nElige una acción:", PODER_KB


def report_audio(_):
    sinks, err = audio_sinks()
    if err:
        return (
            f"<b>🎧 Audio</b>\n❌ <pre>{html_escape(err)}</pre>",
            back_kb("main"),
        )
    if not sinks:
        return "<b>🎧 Audio</b>\nNo se detectaron sinks.", back_kb("main")
    default, _ = audio_default_sink()
    lines = ["<b>🎧 Audio</b>", ""]
    for s in sinks:
        mark = "✅" if s["name"] == default else "⭕"
        lines.append(f"{mark} {s['icon']} <code>{html_escape(s['label'])}</code>")
    lines.append("")
    lines.append(
        "<i>Elige una salida para cambiar el sink por defecto. Lo que esté "
        "sonando ahora también se mueve al sink nuevo.</i>"
    )
    return "\n".join(lines), audio_kb(sinks, default)


def report_pantallas(_):
    outputs, err = niri_outputs()
    if err:
        return (
            f"<b>🖥 Pantallas</b>\n❌ <pre>{html_escape(err)}</pre>",
            back_kb("main"),
        )
    if not outputs:
        return "<b>🖥 Pantallas</b>\nNo se detectaron outputs.", back_kb("main")
    lines = ["<b>🖥 Pantallas</b>", ""]
    for o in outputs:
        icon = "✅" if o["on"] else "⭕"
        line = f"{icon} <code>{html_escape(o['name'])}</code>"
        if o["label"] and o["label"] != o["name"]:
            line += f" — {html_escape(o['label'])}"
        lines.append(line)
    lines.append("")
    lines.append(
        "<i>Tocá un output para apagarlo o encenderlo. DPMS apaga todas las "
        "pantallas sin reordenar el layout (ideal para irte un rato).</i>"
    )
    return "\n".join(lines), pantallas_kb(outputs)


def _media_selected(ctx, players: list[str]) -> str:
    sel = getattr(ctx, "media_selected", "")
    if sel in players:
        return sel
    return players[0] if players else ""


def report_media(ctx):
    players = mpris_players(ctx.cfg)
    if not players:
        return (
            "<b>🎚 Media</b>\n"
            "No hay reproductores MPRIS activos.\n"
            "<i>Abre Spotify, Brave (con YouTube/SoundCloud) u otro player MPRIS y refresca.</i>",
            [[{"text": "🔄 Refrescar", "callback_data": "media"},
              {"text": "← Volver", "callback_data": "main"}]],
        )
    selected = _media_selected(ctx, players)
    st = mpris_status(selected)
    icon = "▶️" if st["status"] == "Playing" else ("⏸" if st["status"] == "Paused" else "⏹")
    title = st["title"] or "<sin título>"
    body = [f"<b>🎚 Media</b> — <code>{html_escape(selected)}</code>", ""]
    body.append(f"{icon} <b>{html_escape(title)}</b>")
    if st["artist"]:
        body.append(f"   {html_escape(st['artist'])}")
    if st["album"]:
        body.append(f"   <i>{html_escape(st['album'])}</i>")
    if len(players) > 1:
        body.append("")
        body.append(f"<i>Players activos: {len(players)}. Elige cuál controlar.</i>")
    return "\n".join(body), media_kb(players, selected, st["status"])


def report_red(_):
    return "<b>📡 Red</b>\nElige una acción:", red_kb()


def report_red_lan(_):
    nbrs = lan_neighbors()
    if not nbrs:
        return "<b>📡 Vecinos LAN</b>\n(sin vecinos)", back_kb("red")
    header = f"{'IP':<16}{'MAC':<19}{'IF':<8}STATE"
    rows = [header]
    for n in nbrs[:30]:
        rows.append(f"{n['ip']:<16}{n['mac']:<19}{n['dev']:<8}{n['state']}")
    return (
        f"<b>📡 Vecinos LAN</b>\n<pre>{html_escape(chr(10).join(rows))}</pre>",
        back_kb("red"),
    )


def report_red_scan(_):
    body = lan_scan_pretty()
    return (
        f"<b>🔭 Scan</b>\n<pre>{html_escape(body[:3500])}</pre>",
        back_kb("red"),
    )


def report_red_wake(ctx):
    targets = (ctx.cfg.get("red") or {}).get("wake") or {}
    if not targets:
        return (
            "<b>⚡ Wake-on-LAN</b>\n"
            "No hay hosts configurados.\n"
            "Agrega entradas <code>[red.wake]</code> en <code>config.toml</code> "
            "con <code>nombre = \"MAC\"</code>.",
            back_kb("red"),
        )
    rows = [[{"text": f"⚡ {name}", "callback_data": f"red:wake:{name}"}] for name in targets]
    rows.append([{"text": "← Volver", "callback_data": "red"}])
    return "<b>⚡ Wake-on-LAN</b>\nElige un host:", rows


def report_vps(ctx):
    cfg_vps = (ctx.cfg.get("vps") or {}).get("hosts") or {}
    if not cfg_vps:
        return (
            "<b>🛸 VPS</b>\n"
            "No hay VPS configurados.\n"
            "Agrega <code>[vps.hosts.&lt;alias&gt;]</code> con <code>ssh_alias = \"…\"</code> "
            "y opcional <code>summary_cmd = \"…\"</code>.",
            back_kb("main"),
        )
    return "<b>🛸 VPS</b>\nElige un host:", vps_kb(ctx.cfg)


def report_vps_status(ctx, alias: str):
    out, err = vps_status(ctx.cfg, alias)
    if err:
        return f"<b>🛸 {html_escape(alias)}</b>\n❌ {html_escape(err)}", back_kb("vps")
    body = out.strip() or "(sin output)"
    return (
        f"<b>🛸 {html_escape(alias)}</b>\n<pre>{html_escape(body[:3500])}</pre>",
        [[{"text": "🔄 Refrescar", "callback_data": f"vps:show:{alias}"},
          {"text": "← Volver", "callback_data": "vps"}]],
    )


def report_games(ctx):
    games = steam_games(ctx.cfg)
    if not games:
        return (
            "<b>🕹 Juegos</b>\n"
            "No se encontraron juegos en Steam (runtimes/Proton excluidos).\n"
            "Instala algo desde Steam y refresca este menú.",
            [[{"text": "🔄 Refrescar", "callback_data": "games"},
              {"text": "← Volver", "callback_data": "main"}]],
        )
    lines = ["<b>🕹 Juegos</b>", ""]
    for g in games:
        lines.append(f"• <code>{g['appid']}</code> — {html_escape(g['name'])}")
    lines.append("")
    use_gs = (ctx.cfg.get("steam") or {}).get("use_gamescope", True)
    lines.append(f"<i>Lanzador: {'gamescope-auto' if use_gs else 'steam directo'}</i>")
    return "\n".join(lines), games_kb(games)


def report_trend(_):
    return "<b>🔮 Tendencia</b>\nElige ventana:", TREND_KB


def report_hosts(ctx):
    return "<b>📡 Otros hosts</b>", hosts_kb(ctx.cfg)


def report_apps(ctx):
    if not ctx.cfg["apps"]:
        return (
            "<b>🪄 Apps</b>\n"
            "No hay apps configuradas. Agregá entradas <code>[apps.&lt;nombre&gt;]</code> "
            "en <code>config.toml</code> con <code>cmd = [...]</code> y opcional "
            "<code>label = \"…\"</code>.",
            apps_kb(ctx.cfg),
        )
    return "<b>🪄 Apps</b>\nElige cuál lanzar:", apps_kb(ctx.cfg)


def report_sistema(ctx):
    m = ctx.metrics
    mem = m.memory()
    la = m.loadavg()
    cpu_pct = m.cpu_pct()
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
    except OSError:
        up = 0
    hostname = run(["hostname"]).strip() or "?"
    kernel = run(["uname", "-r"]).strip() or "?"
    text = (
        f"<b>⚡ Sistema</b>\n"
        f"Host: <code>{html_escape(hostname)}</code>\n"
        f"Kernel: <code>{html_escape(kernel)}</code>\n"
        f"Uptime: <code>{fmt_uptime(up)}</code>\n"
        f"CPU: <code>{cpu_pct:.1f} %</code>  ({m.cpu_count()} cores)\n"
        f"Load: <code>{la[0]:.2f}  {la[1]:.2f}  {la[2]:.2f}</code>\n"
        f"RAM: <code>{mem['used_g']:.1f} / {mem['total_g']:.1f} GiB "
        f"({mem['pct']:.0f} %)</code>"
    )
    return text, back_kb("estado")


def report_disco(ctx):
    rows = ctx.metrics.disk()
    if not rows:
        return "<b>💾 Disco</b>\n(sin datos)", back_kb("estado")
    lines = [f"{'MOUNT':<22} {'USE%':>5}  {'USED':>7} / {'SIZE':>7}"]
    for r in rows:
        lines.append(
            f"{r['mount'][:22]:<22} {r['pct']:>4.0f}%  "
            f"{r['used_g']:>5.1f}G / {r['size_g']:>5.1f}G"
        )
    body = "\n".join(lines)
    return (
        "<b>💾 Disco</b>\n<pre>" + html_escape(body) + "</pre>",
        back_kb("estado"),
    )


def report_red(ctx):
    try:
        raw = subprocess.check_output(
            ["ip", "-j", "-4", "addr"], text=True, timeout=5
        )
        data = json.loads(raw)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError) as e:
        return (
            f"<b>📡 Red</b>\nerror: {html_escape(str(e))}",
            back_kb("estado"),
        )
    lines_iface = []
    for iface in data:
        name = iface.get("ifname", "?")
        if name == "lo":
            continue
        addrs = [
            f"{a.get('local')}/{a.get('prefixlen')}"
            for a in iface.get("addr_info", []) if a.get("local")
        ]
        if not addrs:
            continue
        lines_iface.append(f"{name}: {', '.join(addrs)}")

    tput = ctx.metrics.net_throughput()
    lines_tput = []
    for name, t in tput.items():
        lines_tput.append(
            f"{name:<10} ↓ {fmt_bytes(t['rx_bps'])}/s  ↑ {fmt_bytes(t['tx_bps'])}/s"
        )

    pings = []
    for host in ctx.cfg["network"]["ping_hosts"]:
        out = run(["ping", "-c", "1", "-W", "2", host], timeout=4)
        ok = "1 received" in out or "1 packets received" in out
        ms = ""
        for line in out.splitlines():
            if "time=" in line:
                ms = line.split("time=")[1].split()[0] + " ms"
                break
        pings.append(f"{host}: {'✅ ' + ms if ok else '❌'}")

    body = (
        "Interfaces:\n" + ("\n".join(lines_iface) or "(sin IPs)") +
        "\n\nThroughput:\n" + ("\n".join(lines_tput) or "(sin datos)") +
        "\n\nPing:\n" + "\n".join(pings)
    )
    return (
        "<b>📡 Red</b>\n<pre>" + html_escape(body) + "</pre>",
        back_kb("estado"),
    )


def report_temp(ctx):
    rows = ctx.metrics.temps()
    if not rows:
        return "<b>🔥 Temperaturas</b>\n(sin sensores)", back_kb("estado")
    lines = []
    for r in rows:
        lines.append(f"{r['chip'][:20]:<20} {r['label'][:20]:<20} {r['c']:>5.1f}°C")
    return (
        "<b>🔥 Temperaturas</b>\n<pre>" + html_escape("\n".join(lines)) + "</pre>",
        back_kb("estado"),
    )


def report_gpu(ctx):
    gpus = ctx.metrics.gpus()
    if not gpus:
        return "<b>🩻 GPU</b>\n(sin GPU detectada)", back_kb("estado")
    lines = []
    for g in gpus:
        head = f"{g['name']} — {g['vendor']}"
        lines.append(f"<b>{html_escape(head)}</b>")
        if g["pct"] is not None:
            lines.append(f"  Uso: {g['pct']:.0f} %")
        if g["mem_used_g"] is not None and g["mem_total_g"]:
            mp = g.get("mem_pct") or 0.0
            lines.append(
                f"  VRAM: {g['mem_used_g']:.2f} / {g['mem_total_g']:.2f} GiB ({mp:.0f} %)"
            )
        if g["temp_c"] is not None:
            lines.append(f"  Temp: {g['temp_c']:.1f} °C")
        if g["power_w"] is not None:
            lines.append(f"  Pot: {g['power_w']:.1f} W")
        if g["fan_rpm"] is not None:
            lines.append(f"  Fan: {g['fan_rpm']} rpm")
    return "<b>🩻 GPU</b>\n" + "\n".join(lines), back_kb("estado")


def report_smart(ctx):
    devs = ctx.cfg.get("smart", {}).get("devices", [])
    if not devs:
        return (
            "<b>🩺 SMART</b>\nNo hay discos configurados en <code>[smart] devices</code>.",
            back_kb("estado"),
        )
    if not shutil.which("smartctl"):
        return (
            "<b>🩺 SMART</b>\n<code>smartctl</code> no instalado (paquete <code>smartmontools</code>).",
            back_kb("estado"),
        )
    use_sudo = ctx.cfg.get("sudo", {}).get("enabled", False)
    lines = []
    for dev in devs:
        cmd = ["smartctl", "-H", dev]
        if use_sudo:
            cmd = ["sudo", "-A", *cmd]
        out = run(cmd, timeout=10)
        verdict = "?"
        for line in out.splitlines():
            l = line.strip()
            if l.startswith("SMART overall-health self-assessment test result:"):
                verdict = l.split(":", 1)[1].strip()
                break
            if l.startswith("SMART Health Status:"):
                verdict = l.split(":", 1)[1].strip()
                break
        emoji = "✅" if verdict.lower() in ("passed", "ok") else "⚠"
        lines.append(f"{emoji} {dev}: {verdict}")
    return (
        "<b>🩺 SMART</b>\n<pre>" + html_escape("\n".join(lines)) + "</pre>",
        back_kb("estado"),
    )


def report_top(ctx, by: str):
    rows = top_processes(by)
    header = f"{'PID':>6}  {'USER':<12} {'%CPU':>5} {'%MEM':>5}  COMM"
    body = "\n".join(
        f"{r[0]:>6}  {r[1]:<12} {r[2]:>5} {r[3]:>5}  {r[4]}"
        for r in rows
    ) or "(sin datos)"
    label = "CPU" if by == "cpu" else "RAM"
    ctx.last_top[by] = rows
    return (
        f"<b>💀 Top {label}</b>\n"
        f"<pre>{html_escape(header)}\n{html_escape(body)}</pre>",
        kill_kb(by, rows),
    )


def report_failed(ctx):
    units = list_failed_services()
    out = "(sin servicios fallidos)" if not units else "\n".join(units)
    return (
        "<b>⚙ Servicios fallidos</b>\n<pre>" + html_escape(out[:3500]) + "</pre>",
        back_kb("servicios"),
    )


def report_clave(ctx):
    lines = []
    for s in ctx.cfg["services"]["watch"]:
        state = run(["systemctl", "is-active", s]).strip()
        if state == "active":
            emoji = "✅"
        elif state == "inactive":
            emoji = "⚪"
        else:
            emoji = "❌"
        lines.append(f"{emoji} {s}: {state}")
    return (
        "<b>⚙ Servicios clave</b>\n<pre>"
        + html_escape("\n".join(lines)) + "</pre>",
        back_kb("servicios"),
    )


def report_svc_actions(ctx, svc: str):
    state = run(["systemctl", "is-active", svc]).strip()
    sub = run(["systemctl", "is-enabled", svc]).strip()
    return (
        f"<b>⚙ {html_escape(svc)}</b>\n"
        f"Activo: <code>{html_escape(state)}</code>\n"
        f"Habilitado: <code>{html_escape(sub)}</code>",
        svc_actions_kb(svc),
    )


def report_svc_status(ctx, svc: str):
    out = run([
        "systemctl", "status", svc, "--no-pager", "-n", "20",
    ], timeout=10)
    return (
        f"<b>⚙ {html_escape(svc)} — status</b>\n"
        f"<pre>{html_escape(out[:3500])}</pre>",
        [[{"text": "← Volver", "callback_data": f"svcmenu:{svc}"}]],
    )


def report_logs(ctx):
    out = run([
        "journalctl", "-p", "err", "-n", "30", "--no-pager", "-o", "short",
    ], timeout=10)
    if not out.strip():
        out = "(sin errores recientes)"
    return (
        "<b>📜 Errores recientes (journal)</b>\n"
        f"<pre>{html_escape(out[-3500:])}</pre>",
        back_kb("main"),
    )


UPDATE_UNIT = "pacman-update.service"

# Lock para no disparar dos applies en paralelo (uno desde el botón y
# otro desde /aplicar-updates, por ejemplo).
_update_lock = threading.Lock()
_update_running = False


def report_updates(ctx):
    cmd = ctx.cfg["updates"]["command"]
    env = {**os.environ, "LC_ALL": "C"}

    # Si ya hay un apply corriendo, mostralo en vez de re-chequear.
    if _update_running:
        return (
            "<b>💾 Updates</b>\n⏳ <i>Aplicando updates en este momento…</i>\n"
            "Cuando termine te aviso por separado.",
            [
                [{"text": "📜 Ver log", "callback_data": "updates:log"}],
                [{"text": "← Volver", "callback_data": "main"}],
            ],
        )

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=env,
        )
    except FileNotFoundError:
        return (
            "<b>💾 Updates</b>\n<code>" + html_escape(cmd[0]) +
            "</code> no instalado. En CachyOS: <code>sudo pacman -S pacman-contrib</code>.",
            back_kb("main"),
        )
    except subprocess.TimeoutExpired:
        return (
            "<b>💾 Updates</b>\n<code>" + html_escape(cmd[0]) +
            "</code> tardó más de 30 s — sin respuesta.",
            back_kb("main"),
        )

    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()

    # checkupdates: exit 0 = pendientes listadas; exit 2 = nada pendiente; otro = error.
    if proc.returncode not in (0, 2):
        msg = stderr or stdout or f"exit {proc.returncode}"
        return (
            "<b>💾 Updates</b>\nNo se pudo obtener la lista:\n"
            f"<pre>{html_escape(msg[:2000])}</pre>\n"
            "Suele ser DB de pacman desactualizada o mirrors caídos.",
            back_kb("main"),
        )

    lines = [l for l in stdout.splitlines() if l and "->" in l]
    if not lines:
        body = "Todo al día."
        kb = back_kb("main")
    else:
        body = f"{len(lines)} paquete(s) pendiente(s):\n\n" + "\n".join(lines[:30])
        if len(lines) > 30:
            body += f"\n… y {len(lines) - 30} más"
        kb = [
            [{"text": f"🔄 Aplicar {len(lines)}", "callback_data": "updates:apply"}],
            [{"text": "← Volver", "callback_data": "main"}],
        ]
    return (
        "<b>💾 Updates</b>\n<pre>" + html_escape(body[:3800]) + "</pre>",
        kb,
    )


def report_confirm_apply_updates(ctx):
    # Re-chequeo cuántos hay para mostrar el número en la confirmación.
    cmd = ctx.cfg["updates"]["command"]
    env = {**os.environ, "LC_ALL": "C"}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, env=env,
        )
        lines = [l for l in (proc.stdout or "").splitlines() if l and "->" in l]
        n = len(lines)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        n = 0
    label = f"{n} paquete(s)" if n else "todo lo pendiente"
    return (
        f"<b>⚠️ ¿Aplicar {label}?</b>\n"
        f"Ejecuta <code>pacman -Syu</code> como root vía "
        f"<code>{html_escape(UPDATE_UNIT)}</code>.\n"
        f"Puede tardar varios minutos. Te aviso cuando termine.",
        confirm_kb("upd:apply", cancel_to="updates"),
    )


def report_update_log(ctx):
    out = run(
        ["journalctl", "-u", UPDATE_UNIT, "-n", "40", "--no-pager",
         "-o", "short"],
        timeout=10,
    )
    if not out.strip():
        out = "(sin log aún)"
    return (
        "<b>💾 Update log</b>\n<pre>" + html_escape(out[-3500:]) + "</pre>",
        back_kb("updates"),
    )


def _update_watcher(ctx, started_at: float) -> None:
    """Pollea is-active del pacman-update.service y al terminar manda al
    chat un mensaje con el resultado + últimas líneas del journal."""
    global _update_running
    try:
        deadline = started_at + 35 * 60  # 35 min hard ceiling
        last_state = ""
        while time.time() < deadline:
            time.sleep(5)
            state = run(
                ["systemctl", "is-active", UPDATE_UNIT], timeout=5,
            ).strip()
            last_state = state
            # is-active devuelve "activating" mientras corre el ExecStart
            # de un oneshot. Cuando termina, es "inactive" (ok) o
            # "failed" (algo se rompió).
            if state in ("inactive", "failed", "deactivating"):
                break
        else:
            # timeout del watcher — el unit tiene su propio TimeoutStartSec=30min
            ctx.tg.send(
                f"⚠️ <b>Updates</b>: watcher se rindió tras 35 min. "
                f"Estado actual: <code>{html_escape(last_state)}</code>.\n"
                f"Revisa <code>journalctl -u {UPDATE_UNIT}</code>.",
            )
            ctx.audit.log("updates_apply_watcher_timeout", state=last_state)
            return

        # Resultado real desde systemctl show.
        result = run(
            ["systemctl", "show", UPDATE_UNIT,
             "--property=Result", "--property=ExecMainStatus",
             "--value", "--no-pager"],
            timeout=5,
        ).strip().splitlines()
        exec_result = (result[0] if result else "").strip()
        exec_status = (result[1] if len(result) > 1 else "").strip()
        # Últimas 30 líneas del journal de este unit desde started_at.
        since = datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S")
        log_out = run(
            ["journalctl", "-u", UPDATE_UNIT,
             "--since", since, "--no-pager", "-o", "short", "-n", "30"],
            timeout=10,
        ).strip() or "(sin output)"

        ok = exec_result == "success" and exec_status == "0"
        emoji = "✅" if ok else "❌"
        header = (
            f"{emoji} <b>Updates aplicados</b>" if ok
            else f"{emoji} <b>Updates falló</b> "
                 f"(<code>{html_escape(exec_result)}</code>, "
                 f"exit <code>{html_escape(exec_status)}</code>)"
        )
        try:
            ctx.tg.send(
                f"{header}\n<pre>{html_escape(log_out[-3200:])}</pre>",
                kb=[[{"text": "💾 Updates", "callback_data": "updates"}]],
            )
        except TelegramError:
            pass
        ctx.audit.log(
            "updates_apply_done",
            result=exec_result, exit=exec_status,
            elapsed_s=int(time.time() - started_at),
        )
    finally:
        with _update_lock:
            _update_running = False


def execute_apply_updates(ctx) -> str:
    """Dispara el oneshot pacman-update.service en background y arranca
    el watcher. Devuelve "ok" si systemctl aceptó el start, o el error
    de systemctl si no."""
    global _update_running
    with _update_lock:
        if _update_running:
            return "ya hay un apply corriendo"
        # Sanity: si la unidad existe en disco; si no, fallar limpio.
        # `systemctl cat` devuelve no-cero si no existe.
        try:
            check = subprocess.run(
                ["systemctl", "cat", UPDATE_UNIT],
                capture_output=True, text=True, timeout=5,
            )
            if check.returncode != 0:
                return (
                    f"unit {UPDATE_UNIT} no instalado — corre INSTALL.sh"
                )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "no pude verificar el unit"
        # --no-block: vuelve apenas systemd acepta el job, no espera ExecStart.
        out = run(
            ["systemctl", "start", "--no-block", UPDATE_UNIT], timeout=10,
        ).strip()
        if out and not out.startswith("<exit 0>"):
            return out
        _update_running = True
    started = time.time()
    threading.Thread(
        target=_update_watcher, args=(ctx, started),
        daemon=True, name="update-watcher",
    ).start()
    return "ok"


def report_host(ctx, name: str):
    spec = ctx.cfg["hosts"].get(name, {})
    alias = spec.get("ssh_alias", name)
    if not ctx.ssh:
        return ("<b>🌐 SSH</b>\nSSH no inicializado.", back_kb("hosts"))
    cmd = (
        "uptime; echo ---; "
        "free -h | head -n 2; echo ---; "
        "df -h --output=target,pcent,used,size -x tmpfs -x devtmpfs | head -n 6"
    )
    out = ctx.ssh.run(alias, cmd, timeout=10)
    return (
        f"<b>🌐 {html_escape(name)}</b> (<code>{html_escape(alias)}</code>)\n"
        f"<pre>{html_escape(out[:3500])}</pre>",
        back_kb("hosts"),
    )


def report_trend_window(ctx, secs: int):
    rows = ctx.history.fetch(secs)
    if not rows:
        return (
            "<b>🔮 Tendencia</b>\nAún no hay datos suficientes.",
            back_kb("trend"),
        )
    lab = {3600: "1 h", 21600: "6 h", 86400: "24 h"}.get(secs, f"{secs}s")
    png = render_trend_png(rows, lab)
    if png is None:
        last = rows[-1]
        text = (
            f"<b>🔮 Tendencia ({lab})</b>\n"
            f"matplotlib no disponible — sin gráfico.\n"
            f"Último snapshot:\n"
            f"  CPU: {last[1]:.0f} %\n"
            f"  RAM: {last[2]:.0f} %\n"
            f"  GPU: {(last[3] or 0):.0f} %\n"
            f"  T CPU: {(last[4] or 0):.0f} °C\n"
            f"  T GPU: {(last[5] or 0):.0f} °C\n"
        )
        return text, back_kb("trend")
    try:
        ctx.tg.send_photo(png, caption=f"<b>🔮 Tendencia — {lab}</b>")
    except TelegramError as e:
        return (
            f"<b>🔮 Tendencia</b>\nerror enviando foto: {html_escape(str(e))}",
            back_kb("trend"),
        )
    return (
        f"<b>🔮 Tendencia</b>\nGrafiqué los últimos {lab}.",
        back_kb("trend"),
    )


def report_confirm_power(action: str):
    label = POWER_LABELS.get(action, action)
    if action == "lock":
        text = "<b>⚠️ ¿Bloquear la sesión en la torre?</b>"
    else:
        text = (
            f"<b>⚠️ ¿{label.capitalize()} la torre?</b>\n"
            f"Esta acción se ejecuta inmediatamente."
        )
    return text, confirm_kb(f"pwr:{action}", cancel_to="poder")


def report_confirm_kill(pid: str):
    info = run(["ps", "-o", "pid,user:12,comm,args", "-p", pid, "--no-headers"])
    return (
        f"<b>⚠️ Mandar SIGTERM a PID {html_escape(pid)}?</b>\n"
        f"<pre>{html_escape(info[:1500])}</pre>",
        confirm_kb(f"kill:{pid}", cancel_to="procesos"),
    )


def report_confirm_svc(svc: str, action: str):
    return (
        f"<b>⚠️ ¿{action} {html_escape(svc)}?</b>",
        confirm_kb(f"svc:{svc}:{action}", cancel_to=f"svcmenu:{svc}"),
    )


# ─── Acciones ────────────────────────────────────────────────────────────────

def execute_power(action: str) -> str:
    cmds = {
        "off": ["systemctl", "poweroff"],
        "reboot": ["systemctl", "reboot"],
        "suspend": ["systemctl", "suspend"],
        "lock": ["loginctl", "lock-sessions"],
    }
    cmd = cmds.get(action)
    if cmd is None:
        return "acción desconocida"
    try:
        subprocess.run(
            cmd, check=True, timeout=10,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return "ok"
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or b"").decode("utf-8", errors="replace")
        return err.strip() or f"exit {e.returncode}"
    except subprocess.TimeoutExpired:
        return "timeout"
    except FileNotFoundError:
        return f"no encontrado: {cmd[0]}"


def execute_kill(pid: str) -> str:
    try:
        n = int(pid)
    except ValueError:
        return "pid inválido"
    try:
        os.kill(n, 15)
        return "ok"
    except ProcessLookupError:
        return "proceso ya no existe"
    except PermissionError:
        return "sin permiso (no es proceso de sergioc)"
    except OSError as e:
        return str(e)


def execute_svc(svc: str, action: str, cfg: dict) -> str:
    if svc not in cfg["services"]["manageable"]:
        return "servicio no whitelisteado"
    if action not in ("start", "stop", "restart"):
        return "acción inválida"
    out = run(["systemctl", action, svc], timeout=20)
    return "ok" if not out.strip() or out.strip().startswith("<exit 0>") else out.strip()


def execute_app(name: str, cfg: dict) -> str:
    """Lanza una app GUI vía systemd-run --user contra el systemd manager
    del usuario. Solo funciona si hay una sesión user activa (linger off
    + sin sesión gráfica = falla limpia con 'Failed to connect to bus')."""
    spec = cfg["apps"].get(name)
    if not isinstance(spec, dict):
        return "app no whitelisteada"
    cmd_list = spec.get("cmd")
    if not isinstance(cmd_list, list) or not cmd_list:
        return "config inválida: falta 'cmd' (lista no vacía)"
    if not all(isinstance(p, str) for p in cmd_list):
        return "config inválida: 'cmd' debe ser lista de strings"
    uid = os.getuid()
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "LC_ALL": "C",
    }
    full = [
        "systemd-run", "--user", "--collect", "--quiet",
        "--",
        *cmd_list,
    ]
    try:
        proc = subprocess.run(
            full, capture_output=True, text=True, timeout=8, env=env,
        )
    except subprocess.TimeoutExpired:
        return "timeout esperando a systemd-run"
    except FileNotFoundError:
        return "systemd-run no encontrado"
    if proc.returncode == 0:
        return "ok"
    err = (proc.stderr or proc.stdout or "").strip()
    return err or f"exit {proc.returncode}"


# ─── Routing ─────────────────────────────────────────────────────────────────

ROUTES = {
    "main":             report_main,
    "estado":           report_estado,
    "estado:sistema":   report_sistema,
    "estado:disco":     report_disco,
    "estado:red":       report_red,
    "estado:temp":      report_temp,
    "estado:gpu":       report_gpu,
    "estado:smart":     report_smart,
    "procesos":         report_procesos,
    "procesos:cpu":     lambda ctx: report_top(ctx, "cpu"),
    "procesos:ram":     lambda ctx: report_top(ctx, "ram"),
    "servicios":        report_servicios,
    "servicios:failed": report_failed,
    "servicios:clave":  report_clave,
    "servicios:manage": report_servicios_manage,
    "logs":             report_logs,
    "updates":          report_updates,
    "updates:apply":    report_confirm_apply_updates,
    "updates:log":      report_update_log,
    "trend":            report_trend,
    "hosts":            report_hosts,
    "apps":             report_apps,
    "pantallas":        report_pantallas,
    "audio":            report_audio,
    "media":            report_media,
    "red":              report_red,
    "red:lan":          report_red_lan,
    "red:scan":         report_red_scan,
    "red:wake":         report_red_wake,
    "vps":              report_vps,
    "games":            report_games,
    "poder":            report_poder,
    "poder:off":        lambda ctx: report_confirm_power("off"),
    "poder:reboot":     lambda ctx: report_confirm_power("reboot"),
    "poder:suspend":    lambda ctx: report_confirm_power("suspend"),
    "poder:lock":       lambda ctx: report_confirm_power("lock"),
}


# ─── Context (objeto compartido entre handlers) ──────────────────────────────

class Context:
    def __init__(self, cfg, tg, metrics, history, audit, alerts, ssh):
        self.cfg = cfg
        self.tg = tg
        self.metrics = metrics
        self.history = history
        self.audit = audit
        self.alerts = alerts
        self.ssh = ssh
        self.last_top: dict[str, list] = {}
        self.media_selected: str = ""


# ─── Callback handler ────────────────────────────────────────────────────────

def handle_callback(cq: dict, ctx: Context) -> None:
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    msg_id = msg.get("message_id")
    cb_id = cq.get("id", "")
    tg = ctx.tg

    if not msg_id:
        tg.answer(cb_id, "Mensaje no encontrado")
        return

    ctx.audit.log("callback", data=data)

    if data.startswith("confirm:"):
        token = data.split(":", 1)[1]
        if token.startswith("pwr:"):
            action = token[4:]
            label = POWER_LABELS.get(action, action)
            tg.answer(cb_id, f"Ejecutando: {label}…")
            try:
                tg.edit(msg_id, f"⏳ Ejecutando <b>{html_escape(label)}</b>…")
            except TelegramError:
                pass
            ctx.audit.log("power", action=action)
            result = execute_power(action)
            try:
                if result == "ok":
                    tg.edit(
                        msg_id,
                        f"✅ <b>{html_escape(label.capitalize())}</b> completado.",
                        kb=back_kb("main"),
                    )
                else:
                    tg.edit(
                        msg_id,
                        f"❌ <b>{html_escape(label.capitalize())}</b> falló:\n"
                        f"<pre>{html_escape(result[:3500])}</pre>",
                        kb=back_kb("main"),
                    )
            except TelegramError:
                pass
            return
        if token.startswith("kill:"):
            pid = token[5:]
            tg.answer(cb_id, f"Matando {pid}…")
            ctx.audit.log("kill", pid=pid)
            result = execute_kill(pid)
            try:
                tg.edit(
                    msg_id,
                    f"{'✅' if result == 'ok' else '❌'} kill {html_escape(pid)}: "
                    f"<code>{html_escape(result)}</code>",
                    kb=back_kb("procesos"),
                )
            except TelegramError:
                pass
            return
        if token.startswith("svc:"):
            _, svc, action = token.split(":", 2)
            tg.answer(cb_id, f"{action} {svc}…")
            ctx.audit.log("svc", service=svc, action=action)
            result = execute_svc(svc, action, ctx.cfg)
            try:
                tg.edit(
                    msg_id,
                    f"{'✅' if result == 'ok' else '❌'} {html_escape(action)} "
                    f"<code>{html_escape(svc)}</code>:\n"
                    f"<pre>{html_escape(result[:2000])}</pre>",
                    kb=[[{"text": "← Volver", "callback_data": f"svcmenu:{svc}"}]],
                )
            except TelegramError:
                pass
            return
        if token == "upd:apply":
            tg.answer(cb_id, "Aplicando updates…")
            try:
                tg.edit(
                    msg_id,
                    "⏳ <b>Aplicando updates…</b>\n"
                    "Esto puede tomar varios minutos. Te aviso cuando termine.",
                    kb=[[{"text": "📜 Ver log", "callback_data": "updates:log"}]],
                )
            except TelegramError:
                pass
            ctx.audit.log("updates_apply_start")
            result = execute_apply_updates(ctx)
            if result != "ok":
                try:
                    tg.edit(
                        msg_id,
                        f"❌ <b>No se pudo lanzar el update</b>:\n"
                        f"<pre>{html_escape(result[:2000])}</pre>",
                        kb=back_kb("updates"),
                    )
                except TelegramError:
                    pass
                ctx.audit.log("updates_apply_start_failed", error=result)
            return
        if token.startswith("snooze:"):
            key = token.split(":", 1)[1]
            ctx.alerts.snooze(key, 3600)
            ctx.audit.log("snooze", alert=key, seconds=3600)
            try:
                tg.edit_kb(msg_id, [[{"text": "🔕 silenciado 1 h", "callback_data": "noop"}]])
            except TelegramError:
                pass
            tg.answer(cb_id, "Silenciado 1 h")
            return
        tg.answer(cb_id, "Confirmación desconocida")
        return

    if data.startswith("snooze:"):
        key = data.split(":", 1)[1]
        ctx.alerts.snooze(key, 3600)
        ctx.audit.log("snooze", alert=key, seconds=3600)
        try:
            tg.edit_kb(msg_id, [[{"text": "🔕 silenciado 1 h", "callback_data": "noop"}]])
        except TelegramError:
            pass
        tg.answer(cb_id, "Silenciado 1 h")
        return

    if data.startswith("trend:"):
        secs = int(data.split(":", 1)[1])
        tg.answer(cb_id)
        text, kb = report_trend_window(ctx, secs)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("host:"):
        name = data.split(":", 1)[1]
        tg.answer(cb_id)
        text, kb = report_host(ctx, name)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("app:"):
        name = data.split(":", 1)[1]
        spec = ctx.cfg["apps"].get(name) or {}
        label = spec.get("label") or name
        tg.answer(cb_id, f"Lanzando {label}…")
        try:
            tg.edit(msg_id, f"⏳ Lanzando <b>{html_escape(label)}</b>…")
        except TelegramError:
            pass
        result = execute_app(name, ctx.cfg)
        ctx.audit.log("app_launch", name=name, result=result)
        try:
            if result == "ok":
                tg.edit(
                    msg_id,
                    f"✅ <b>{html_escape(label)}</b> lanzada.",
                    kb=back_kb("apps"),
                )
            else:
                tg.edit(
                    msg_id,
                    f"❌ <b>{html_escape(label)}</b> falló:\n"
                    f"<pre>{html_escape(result[:2000])}</pre>",
                    kb=back_kb("apps"),
                )
        except TelegramError:
            pass
        return

    if data.startswith("svcmenu:"):
        svc = data.split(":", 1)[1]
        tg.answer(cb_id)
        text, kb = report_svc_actions(ctx, svc)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("svcact:"):
        _, svc, action = data.split(":", 2)
        tg.answer(cb_id)
        if action == "status":
            text, kb = report_svc_status(ctx, svc)
        else:
            text, kb = report_confirm_svc(svc, action)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("killask:"):
        pid = data.split(":", 1)[1]
        tg.answer(cb_id)
        text, kb = report_confirm_kill(pid)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("audio:set:"):
        sink = data.split(":", 2)[2]
        result = audio_set_sink(sink)
        if result == "ok":
            tg.answer(cb_id, "Audio cambiado ✅")
        else:
            tg.answer(cb_id, f"❌ {result[:180]}")
        ctx.audit.log("audio_set", sink=sink, result=result)
        text, kb = report_audio(ctx)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("pant:"):
        rest = data[5:]
        if rest in ("dpms_off", "dpms_on"):
            on = (rest == "dpms_on")
            result = niri_dpms(on)
            label = "DPMS on" if on else "DPMS off"
            tg.answer(cb_id, f"{label} ✅" if result == "ok" else f"❌ {result[:180]}")
            ctx.audit.log("pantallas_dpms", action=rest, result=result)
        elif ":" in rest and rest.split(":", 1)[0] in ("on", "off"):
            action, name = rest.split(":", 1)
            result = niri_set_output(name, action == "on")
            tg.answer(
                cb_id,
                f"{name}: {action} ✅" if result == "ok" else f"❌ {result[:180]}",
            )
            ctx.audit.log("pantallas_set", output=name, action=action, result=result)
        else:
            tg.answer(cb_id, "Acción desconocida")
            return
        text, kb = report_pantallas(ctx)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("media:"):
        rest = data.split(":", 1)[1]
        players = mpris_players(ctx.cfg)
        if not players:
            tg.answer(cb_id, "Sin players activos")
            text, kb = report_media(ctx)
            try:
                tg.edit(msg_id, text, kb=kb)
            except TelegramError:
                pass
            return
        if rest.startswith("pick:"):
            try:
                idx = int(rest.split(":", 1)[1])
                if 0 <= idx < len(players):
                    ctx.media_selected = players[idx]
                    tg.answer(cb_id, f"Player: {players[idx][:30]}")
            except ValueError:
                tg.answer(cb_id, "Índice inválido")
        else:
            selected = _media_selected(ctx, players)
            action_map = {
                "toggle": "play-pause",
                "next": "next",
                "prev": "previous",
                "vol+": "vol+",
                "vol-": "vol-",
            }
            if rest.startswith("seek:"):
                # rest = "seek:+10" → mpris_action espera "seek:+10".
                act = rest
            else:
                act = action_map.get(rest)
            if act is None:
                tg.answer(cb_id, "Acción desconocida")
                return
            result = mpris_action(act, selected)
            tg.answer(cb_id, "✅" if result == "ok" else f"❌ {result[:120]}")
            ctx.audit.log("media_action", action=rest, player=selected, result=result)
        text, kb = report_media(ctx)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("red:wake:"):
        target = data.split(":", 2)[2]
        result = wol_send(target, ctx.cfg)
        tg.answer(cb_id, f"⚡ {target}: {result[:60]}")
        ctx.audit.log("wol_send", target=target, result=result)
        text, kb = report_red_wake(ctx)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("vps:show:"):
        alias = data.split(":", 2)[2]
        tg.answer(cb_id, f"Consultando {alias}…")
        ctx.audit.log("vps_query", alias=alias)
        text, kb = report_vps_status(ctx, alias)
        try:
            tg.edit(msg_id, text, kb=kb)
        except TelegramError:
            pass
        return

    if data.startswith("game:launch:"):
        appid = data.split(":", 2)[2]
        tg.answer(cb_id, f"Lanzando {appid}…")
        try:
            tg.edit(msg_id, f"⏳ Lanzando juego <code>{html_escape(appid)}</code>…")
        except TelegramError:
            pass
        result = steam_launch(appid, ctx.cfg)
        ctx.audit.log("game_launch", appid=appid, result=result)
        try:
            if result == "ok":
                tg.edit(
                    msg_id,
                    f"✅ Juego <code>{html_escape(appid)}</code> lanzado.",
                    kb=back_kb("games"),
                )
            else:
                tg.edit(
                    msg_id,
                    f"❌ Juego <code>{html_escape(appid)}</code> falló:\n"
                    f"<pre>{html_escape(result[:2000])}</pre>",
                    kb=back_kb("games"),
                )
        except TelegramError:
            pass
        return

    if data == "noop":
        tg.answer(cb_id)
        return

    handler = ROUTES.get(data)
    if not handler:
        tg.answer(cb_id, "Acción desconocida")
        return
    tg.answer(cb_id)
    try:
        text, kb = handler(ctx)
        tg.edit(msg_id, text, kb=kb)
    except TelegramError:
        pass
    except Exception as e:
        try:
            tg.edit(
                msg_id,
                f"❌ error: <pre>{html_escape(repr(e))}</pre>",
                kb=back_kb("main"),
            )
        except TelegramError:
            pass


SLASH_ROUTES = {
    "/start":      "main",
    "/menu":       "main",
    "/estado":     "estado",
    "/procesos":   "procesos",
    "/servicios":  "servicios",
    "/logs":       "logs",
    "/tendencia":  "trend",
    "/updates":    "updates",
    "/hosts":      "hosts",
    "/apps":       "apps",
    "/pantallas":  "pantallas",
    "/audio":      "audio",
    "/media":      "media",
    "/red":        "red",
    "/vps":        "vps",
    "/games":      "games",
    "/poder":      "poder",
}

BOT_COMMANDS = [
    {"command": "menu",      "description": "Abrir menú principal"},
    {"command": "estado",    "description": "Estado del sistema"},
    {"command": "procesos",  "description": "Top procesos (CPU / RAM)"},
    {"command": "servicios", "description": "Estado y control de servicios"},
    {"command": "logs",      "description": "Logs recientes"},
    {"command": "tendencia", "description": "Gráficas de tendencia"},
    {"command": "updates",   "description": "Updates pendientes (lista + aplicar)"},
    {"command": "hosts",     "description": "Otros hosts (vps)"},
    {"command": "apps",      "description": "Lanzar aplicaciones GUI"},
    {"command": "pantallas", "description": "Encender / apagar monitores"},
    {"command": "audio",     "description": "Cambiar la salida de audio"},
    {"command": "media",     "description": "Reproductores MPRIS (play/pause/vol)"},
    {"command": "red",       "description": "Vecinos LAN, scan, Wake-on-LAN"},
    {"command": "scan",      "description": "Scan rápido de la LAN"},
    {"command": "wake",      "description": "Wake-on-LAN: /wake <host>"},
    {"command": "vps",       "description": "Estado de VPS por SSH"},
    {"command": "games",     "description": "Lanzar juegos de Steam"},
    {"command": "nota",      "description": "Anotar al inbox de Obsidian: /nota <texto>"},
    {"command": "poder",     "description": "Apagar / reiniciar / suspender / bloquear"},
    {"command": "ping",      "description": "Health check"},
    {"command": "help",      "description": "Ayuda"},
]


def handle_message(msg: dict, ctx: Context) -> None:
    text = (msg.get("text") or "").strip()
    tg = ctx.tg
    cmd = text.split()[0].split("@", 1)[0] if text else ""
    if cmd in SLASH_ROUTES:
        handler = ROUTES.get(SLASH_ROUTES[cmd])
        if handler is not None:
            try:
                t, kb = handler(ctx)
                tg.send(t, kb=kb)
            except TelegramError:
                pass
            except Exception as e:
                try:
                    tg.send(f"❌ error: <pre>{html_escape(repr(e))}</pre>")
                except TelegramError:
                    pass
        return
    if cmd == "/ping":
        try:
            tg.send("pong")
        except TelegramError:
            pass
        return
    if cmd == "/nota":
        rest = text[len("/nota"):].strip()
        if not rest:
            try:
                tg.send("Uso: <code>/nota &lt;texto&gt;</code>")
            except TelegramError:
                pass
            return
        result = note_append(rest, ctx.cfg)
        ctx.audit.log("note_added", chars=len(rest), result=result)
        try:
            if result == "ok":
                tg.send(f"📝 Nota agregada al inbox ({len(rest)} chars).")
            else:
                tg.send(f"❌ {html_escape(result)}")
        except TelegramError:
            pass
        return
    if cmd == "/wake":
        target = text[len("/wake"):].strip()
        if not target:
            targets = (ctx.cfg.get("red") or {}).get("wake") or {}
            hosts = ", ".join(targets) or "(ninguno configurado)"
            try:
                tg.send(f"Uso: <code>/wake &lt;host&gt;</code>\nHosts: {html_escape(hosts)}")
            except TelegramError:
                pass
            return
        result = wol_send(target, ctx.cfg)
        ctx.audit.log("wol_send", target=target, result=result)
        try:
            tg.send(f"⚡ <code>{html_escape(target)}</code>: {html_escape(result)}")
        except TelegramError:
            pass
        return
    if cmd == "/scan":
        body = lan_scan_pretty()
        try:
            tg.send(f"<b>🔭 Scan</b>\n<pre>{html_escape(body[:3500])}</pre>")
        except TelegramError:
            pass
        return
    if cmd == "/help":
        try:
            lines = ["Comandos:"]
            for c in BOT_COMMANDS:
                lines.append(f"/{c['command']} — {c['description']}")
            lines.append("\nTodo lo demás se navega por botones.")
            tg.send("\n".join(lines))
        except TelegramError:
            pass
        return


# ─── Polling ─────────────────────────────────────────────────────────────────

def poll_loop(tg: Telegram, ctx: Context, poll_timeout: int) -> None:
    offset = 0
    try:
        old = tg.updates(offset=-1, timeout=0)
        if old:
            offset = old[-1]["update_id"] + 1
    except TelegramError as e:
        print(f"poll: initial drain failed: {e}", file=sys.stderr)

    while True:
        try:
            updates = tg.updates(offset=offset, timeout=poll_timeout)
        except TelegramError as e:
            print(f"poll: {e}", file=sys.stderr)
            time.sleep(2)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            if "callback_query" in update:
                cq = update["callback_query"]
                msg = cq.get("message") or {}
                if msg.get("chat", {}).get("id") != tg.chat_id:
                    tg.answer(cq.get("id", ""), "Chat no autorizado")
                    continue
                try:
                    handle_callback(cq, ctx)
                except Exception as e:
                    print(f"callback handler crashed: {e!r}", file=sys.stderr)
            elif "message" in update:
                m = update["message"]
                if m.get("chat", {}).get("id") != tg.chat_id:
                    continue
                try:
                    handle_message(m, ctx)
                except Exception as e:
                    print(f"message handler crashed: {e!r}", file=sys.stderr)


# ─── Monitor (alertas + eventos + histórico) ─────────────────────────────────

def fire_alert(ctx: Context, key: str, value: float, snap: dict) -> None:
    titles = {
        "cpu_pct":        ("⚠️ CPU alta",        f"{value:.0f} %"),
        "ram_pct":        ("⚠️ RAM alta",        f"{value:.0f} %"),
        "disk_pct":       ("⚠️ Disco lleno",     f"{value:.0f} %"),
        "cpu_temp_c":     ("🔥 CPU caliente",    f"{value:.0f} °C"),
        "gpu_temp_c":     ("🔥 GPU caliente",    f"{value:.0f} °C"),
        "gpu_pct":        ("⚠️ GPU saturada",    f"{value:.0f} %"),
        "load1_per_core": ("⚠️ Load alta",       f"{value:.2f}/core"),
    }
    title, val = titles.get(key, (key, str(value)))
    by = "cpu" if key in ("cpu_pct", "cpu_temp_c", "load1_per_core") else "ram"
    if key == "gpu_pct" or key == "gpu_temp_c":
        body = ""
    else:
        rows = top_processes(by, n=5)
        header = f"{'PID':>6}  {'USER':<10} {'%CPU':>5} {'%MEM':>5}  COMM"
        body_lines = "\n".join(
            f"{r[0]:>6}  {r[1]:<10} {r[2]:>5} {r[3]:>5}  {r[4]}" for r in rows
        )
        body = f"\nTop procesos:\n<pre>{html_escape(header)}\n{html_escape(body_lines)}</pre>"
    if key == "disk_pct":
        mount = snap.get("disk_mount") or "?"
        val += f" ({mount})"
    text = f"<b>{title}</b>\nValor: <code>{val}</code>{body}"
    kb = [[{"text": "🔕 Silenciar 1 h", "callback_data": f"snooze:{key}"}]]
    try:
        ctx.tg.send(text, kb=kb)
    except TelegramError as e:
        print(f"alert: send failed: {e}", file=sys.stderr)
    ctx.audit.log("alert", key=key, value=value)


def check_events(ctx: Context, state: dict) -> None:
    failed_now = set(list_failed_services())
    new_failed = failed_now - state["failed_services"]
    if new_failed and state["failed_services_seen"]:
        for svc in sorted(new_failed):
            try:
                ctx.tg.send(f"❌ Servicio falló: <code>{html_escape(svc)}</code>")
            except TelegramError:
                pass
            ctx.audit.log("service_failed", service=svc)
    state["failed_services"] = failed_now
    state["failed_services_seen"] = True

    sessions_now = list_sessions()
    new_sess = sessions_now - state["sessions"]
    if new_sess and state["sessions_seen"]:
        for s in sorted(new_sess):
            try:
                ctx.tg.send(f"👤 Nueva sesión: <code>{html_escape(s)}</code>")
            except TelegramError:
                pass
            ctx.audit.log("session_new", session=s)
    state["sessions"] = sessions_now
    state["sessions_seen"] = True


def monitor_loop(ctx: Context) -> None:
    cfg = ctx.cfg
    interval = int(cfg["monitor"]["interval_s"])
    state = {
        "failed_services": set(),
        "failed_services_seen": False,
        "sessions": set(),
        "sessions_seen": False,
        "last_tick": time.monotonic(),
    }

    ctx.metrics.cpu_pct()
    ctx.metrics.net_throughput()
    time.sleep(2)

    last_prune = 0
    while True:
        t0 = time.monotonic()
        wall_skip = t0 - state["last_tick"] - interval
        if wall_skip > 60 and state["last_tick"] != 0:
            try:
                ctx.tg.send(
                    f"💤→🟢 Volví de suspend (gap ≈ {int(wall_skip)} s)"
                )
            except TelegramError:
                pass
            ctx.audit.log("resume", gap_s=wall_skip)
        state["last_tick"] = t0

        try:
            cpu = ctx.metrics.cpu_pct()
            mem = ctx.metrics.memory()
            disk_mount, disk_pct = ctx.metrics.disk_max_pct()
            cpu_temp = ctx.metrics.cpu_temp_max()
            gpu_temp = ctx.metrics.gpu_max_temp()
            gpu_pct = ctx.metrics.gpu_max_pct()
            la = ctx.metrics.loadavg()
            cores = ctx.metrics.cpu_count()
            load_per_core = la[0] / cores if cores else 0.0

            snap = {
                "cpu_pct": cpu, "ram_pct": mem["pct"],
                "gpu_pct": gpu_pct, "cpu_temp": cpu_temp,
                "gpu_temp": gpu_temp, "load1": la[0],
                "disk_pct": disk_pct, "disk_mount": disk_mount,
            }
            ctx.history.insert(int(time.time()), snap)

            for key, val in (
                ("cpu_pct", cpu), ("ram_pct", mem["pct"]),
                ("disk_pct", disk_pct), ("cpu_temp_c", cpu_temp),
                ("gpu_temp_c", gpu_temp), ("gpu_pct", gpu_pct),
                ("load1_per_core", load_per_core),
            ):
                if ctx.alerts.evaluate(key, val):
                    fire_alert(ctx, key, val, snap)

            check_events(ctx, state)

            now = time.time()
            if now - last_prune > 6 * 3600:
                ctx.history.prune()
                last_prune = now
        except Exception as e:
            print(f"monitor: tick failed: {e!r}", file=sys.stderr)

        elapsed = time.monotonic() - t0
        time.sleep(max(1.0, interval - elapsed))


# ─── Validación whitelist polkit ↔ config ────────────────────────────────────

POLKIT_RULE_PATH = "/etc/polkit-1/rules.d/50-bot-comandos-torre.rules"


def polkit_allowed_units(path: str = POLKIT_RULE_PATH) -> set[str] | None:
    """Extrae las units listadas en `allowedUnits` de la rule polkit.
    Devuelve None si no se puede leer (típico: el daemon corre como sergioc
    y /etc/polkit-1/rules.d es 0750 root:polkitd — no fatal, la validación
    cruzada la hace INSTALL.sh con privilegios de root)."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    m = re.search(r"allowedUnits\s*=\s*\{([^}]*)\}", text, re.DOTALL)
    if not m:
        return None
    return set(re.findall(r'"([^"]+)"\s*:\s*true', m.group(1)))


def warn_whitelist_drift(cfg: dict) -> None:
    """Mejor esfuerzo: si por casualidad el daemon SÍ puede leer la rule
    (admin la dejó world-readable, o el directorio relajado), reportamos
    drift entre [services].manageable y la rule polkit. Si no la puede
    leer, silencio total — INSTALL.sh ya hace esta validación al instalar."""
    allowed = polkit_allowed_units()
    if allowed is None:
        return
    missing = []
    for svc in cfg["services"]["manageable"]:
        unit = svc if "." in svc else f"{svc}.service"
        if unit not in allowed:
            missing.append(unit)
    if missing:
        print(
            "warn: estos servicios están en [services].manageable pero NO en "
            f"la rule polkit ({POLKIT_RULE_PATH}); start/stop/restart fallará "
            f"con auth required: {', '.join(missing)}",
            file=sys.stderr,
        )


# ─── main ────────────────────────────────────────────────────────────────────

BOOT_QUOTES = [
    "Wake up, samurai. We have a city to burn.",
    "The sky above the port was the color of television, tuned to a dead channel.",
    "Plug in. Jack out.",
    "The body is just chrome.",
    "When the BLACKWALL flickers, runners get rich.",
    "ICE-breakers warming up.",
    "Output set. Stream open. We're in.",
    "Net is wide. Watch your six.",
    "Cyberspace: a consensual hallucination.",
    "Code is poetry. Exploits are art.",
    "Stack the deck. Burn the rig.",
    "NetWatch sleeps. Runners don't.",
]


def main() -> None:
    cfg = load_config(CONFIG_PATH)
    warn_whitelist_drift(cfg)
    tg = Telegram(cfg["bot_token"], cfg["chat_id"])
    metrics = Metrics()
    history = History(cfg["history"]["db_path"], cfg["history"]["retain_days"])
    audit = Audit(cfg["audit"]["log_path"])
    alerts = Alerts(cfg)
    ssh = SSHRunner(cfg["ssh"]["known_hosts"]) if cfg["hosts"] else None

    reaper = MessageReaper(
        tg,
        cfg["chat_auto_delete_s"],
        cfg["chat_auto_delete_interval_s"],
        audit,
    )
    tg.reaper = reaper
    if reaper.enabled:
        threading.Thread(
            target=reaper.run, daemon=True, name="reaper"
        ).start()

    ctx = Context(cfg, tg, metrics, history, audit, alerts, ssh)

    audit.log(
        "start",
        chat_id=cfg["chat_id"],
        pid=os.getpid(),
        chat_auto_delete_s=cfg["chat_auto_delete_s"],
    )
    print(
        f"bot-comandos-torre listening (chat_id={cfg['chat_id']}, "
        f"monitor={cfg['monitor']['enabled']})",
        file=sys.stderr,
    )

    tg.set_commands(BOT_COMMANDS)
    tg.set_menu_button()

    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot_id = f.read().strip()
        state_dir = Path(cfg["history"]["db_path"]).parent
        boot_marker = state_dir / "last_boot_id"
        last_boot_id = ""
        if boot_marker.exists():
            try:
                last_boot_id = boot_marker.read_text().strip()
            except OSError:
                pass
        if boot_id and boot_id != last_boot_id:
            try:
                with open("/proc/uptime") as f:
                    up = float(f.read().split()[0])
            except OSError:
                up = 0.0
            hostname = run(["hostname"]).strip() or "?"
            quote = random.choice(BOOT_QUOTES)
            try:
                tg.send(
                    f"🟢 <b>Torre iniciada</b> en <code>{html_escape(hostname)}</code>\n"
                    f"<i>// {html_escape(quote)}</i>\n"
                    f"Uptime: {fmt_uptime(up)}\n\n"
                    f"Elige una categoría:",
                    kb=MAIN_KB,
                )
            except TelegramError as e:
                # No persistimos el boot_id: queremos reintentar en el próximo
                # startup (típicamente tras Restart=on-failure) en lugar de
                # tragarnos la notificación.
                print(f"boot notify failed, will retry next start: {e}", file=sys.stderr)
            else:
                audit.log("boot", uptime_s=up, boot_id=boot_id)
                try:
                    boot_marker.write_text(boot_id + "\n")
                except OSError as e:
                    print(f"could not persist boot marker: {e}", file=sys.stderr)
    except OSError:
        pass

    if cfg["monitor"]["enabled"]:
        t = threading.Thread(target=monitor_loop, args=(ctx,), daemon=True)
        t.start()

    poll_loop(tg, ctx, cfg["poll_timeout_s"])


if __name__ == "__main__":
    main()
