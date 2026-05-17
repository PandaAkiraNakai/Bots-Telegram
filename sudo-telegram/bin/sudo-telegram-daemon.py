#!/usr/bin/env python3
"""
sudo-telegram daemon.

Listens on a Unix socket for askpass requests, sends an approval prompt
to a Telegram bot (the "approval bot"), waits for the owner to reply
/yes_<uuid> or /no_<uuid>, and on approval returns the sudo password.

Runs as root via systemd. Stdlib only (Python 3.11+ for tomllib).
"""

import grp
import json
import os
import pwd
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

try:
    import tomllib
except ImportError:
    print("Python 3.11+ required (tomllib missing)", file=sys.stderr)
    sys.exit(2)


CONFIG_PATH = os.environ.get("STG_CONFIG", "/etc/sudo-telegram/config.toml")


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for key in ("approval_bot_token", "chat_id", "password_path"):
        if key not in cfg:
            print(f"config missing required key: {key}", file=sys.stderr)
            sys.exit(2)
    cfg.setdefault("socket_path", "/run/sudo-telegram/sock")
    cfg.setdefault("socket_group", "sudo-telegram")
    cfg.setdefault("log_path", "/var/log/sudo-telegram/audit.log")
    cfg.setdefault("timeout_s", 60)
    cfg.setdefault("poll_timeout_s", 25)
    cfg.setdefault("rate_limit_n", 10)
    cfg.setdefault("rate_limit_window_s", 300)
    cfg.setdefault("allowed_users", [])
    cfg.setdefault("dry_run", False)
    # Auto-borrado del chat: borra los mensajes que el bot mandó tras N
    # segundos. 0 desactiva. El reaper chequea cada
    # `chat_auto_delete_interval_s`.
    cfg.setdefault("chat_auto_delete_s", 1800)
    cfg.setdefault("chat_auto_delete_interval_s", 60)
    return cfg


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: int):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = int(chat_id)
        # Reaper se setea desde main() después de construir el daemon. Si
        # queda en None (modo legacy / tests), send_message no rastrea.
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

    def send_message(self, text: str, reply_markup: dict | None = None) -> int:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        result = self._post("sendMessage", payload)
        message_id = result["message_id"]
        if self.reaper is not None:
            self.reaper.track(message_id)
        return message_id

    def delete_message(self, message_id: int) -> None:
        self._post(
            "deleteMessage",
            {"chat_id": self.chat_id, "message_id": int(message_id)},
        )

    def edit_message_text(
        self, message_id: int, text: str, reply_markup: dict | None = None
    ) -> None:
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        self._post("editMessageText", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self._post("answerCallbackQuery", payload)

    def get_updates(self, offset: int, timeout: int) -> list:
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


class PendingRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}

    def register(self, uuid: str, expires_at: float) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._pending[uuid] = {
                "event": ev,
                "decision": None,
                "expires_at": expires_at,
            }
        return ev

    def unregister(self, uuid: str) -> dict | None:
        with self._lock:
            return self._pending.pop(uuid, None)

    def resolve(self, uuid: str, decision: str) -> bool:
        with self._lock:
            entry = self._pending.get(uuid)
            if not entry or entry["decision"] is not None:
                return False
            entry["decision"] = decision
            entry["event"].set()
            return True

    def list_uuids(self) -> list[str]:
        with self._lock:
            return list(self._pending.keys())

    def cleanup_expired(self) -> None:
        now = time.time()
        with self._lock:
            for uuid, entry in self._pending.items():
                if entry["decision"] is None and entry["expires_at"] < now:
                    entry["decision"] = "TIMEOUT"
                    entry["event"].set()


class RateLimiter:
    def __init__(self, n: int, window_s: int):
        self.n = n
        self.window_s = window_s
        self._lock = threading.Lock()
        self._events: deque[float] = deque()

    def check_and_record(self) -> bool:
        now = time.time()
        with self._lock:
            while self._events and self._events[0] < now - self.window_s:
                self._events.popleft()
            if len(self._events) >= self.n:
                return False
            self._events.append(now)
            return True


class MessageReaper:
    """Borra mensajes que el bot mandó tras `delete_after_s` segundos.

    Trackea cada message_id que pasa por send_message en un deque junto
    con su timestamp de envío. Un thread background corre cada
    `interval_s` segundos y borra los que ya cumplieron edad. Borra de
    a uno (Telegram no expone bulk delete) y absorbe errores
    silenciosamente — un mensaje que ya no existe (borrado a mano,
    >48h, etc.) no debe matar el reaper.
    """

    def __init__(self, tg: "Telegram", delete_after_s: int, interval_s: int, audit: "AuditLog"):
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
        """Borra los que vencieron. Devuelve cuántos borró exitosamente."""
        ok = 0
        for mid in self._drain_due():
            try:
                self.tg.delete_message(mid)
                ok += 1
            except TelegramError:
                # Ya borrado, >48h, o el bot perdió permisos. Lo descartamos.
                pass
        if ok:
            self.audit.write({"event": "chat_reaped", "count": ok})
        return ok

    def run(self) -> None:
        while True:
            time.sleep(self.interval_s)
            try:
                self.reap_once()
            except Exception as e:  # defensa de último recurso
                print(f"reaper: {e}", file=sys.stderr)


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict) -> None:
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as e:
                print(f"audit write failed: {e}", file=sys.stderr)


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def read_proc_cmdline(pid: int) -> str:
    """Read /proc/<pid>/cmdline. Daemon runs as root, so this works even
    when sudo set dumpable=0 (root has CAP_SYS_PTRACE)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\0") if p]
        return " ".join(parts)
    except (OSError, ValueError):
        return ""


def decision_keyboard(uuid: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Sí", "callback_data": f"yes_{uuid}"},
            {"text": "❌ No", "callback_data": f"no_{uuid}"},
        ]]
    }


def parse_decision(data: str) -> tuple[str, str] | None:
    """Map 'yes_<uuid>' / 'no_<uuid>' (with optional leading slash and
    @botname suffix) to (decision, uuid). Returns None if malformed."""
    s = data.lstrip("/")
    if s.startswith("yes_"):
        decision, rest = "APPROVED", s[4:]
    elif s.startswith("no_"):
        decision, rest = "DENIED", s[3:]
    else:
        return None
    uuid = rest.split()[0].split("@")[0] if rest else ""
    if not uuid or not all(c in "0123456789abcdef" for c in uuid):
        return None
    return decision, uuid


def telegram_poll_loop(
    tg: Telegram,
    registry: PendingRegistry,
    audit: AuditLog,
    poll_timeout: int,
):
    offset = 0
    try:
        old = tg.get_updates(offset=-1, timeout=0)
        if old:
            offset = old[-1]["update_id"] + 1
    except TelegramError as e:
        print(f"poll: initial drain failed: {e}", file=sys.stderr)

    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout=poll_timeout)
        except TelegramError as e:
            print(f"poll: {e}", file=sys.stderr)
            time.sleep(2)
            registry.cleanup_expired()
            continue

        for update in updates:
            offset = update["update_id"] + 1
            if "callback_query" in update:
                handle_callback(update["callback_query"], tg, registry, audit)
            elif "message" in update:
                msg = update["message"]
                if msg.get("chat", {}).get("id") != tg.chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                handle_text(text, tg, registry, audit)

        registry.cleanup_expired()


def handle_text(text: str, tg: Telegram, registry: PendingRegistry, audit: AuditLog):
    parsed = parse_decision(text)
    if parsed is not None:
        decision, uuid = parsed
        if registry.resolve(uuid, decision):
            # handle_client thread will edit the prompt message; nothing to
            # send here.
            return
        try:
            tg.send_message(
                f"⚠️ <code>{uuid}</code> no está pendiente "
                f"(expirado o desconocido)"
            )
        except TelegramError:
            pass
        audit.write({
            "event": "stale_response",
            "uuid": uuid,
            "decision": decision,
        })
        return
    if text.startswith("/ping"):
        try:
            tg.send_message("pong")
        except TelegramError:
            pass
    elif text.startswith("/list"):
        pending = registry.list_uuids()
        msg = "Pendientes: " + (", ".join(pending) if pending else "(ninguno)")
        try:
            tg.send_message(msg)
        except TelegramError:
            pass


def handle_callback(
    cq: dict, tg: Telegram, registry: PendingRegistry, audit: AuditLog
):
    cb_id = cq.get("id", "")
    msg = cq.get("message") or {}
    if msg.get("chat", {}).get("id") != tg.chat_id:
        try:
            tg.answer_callback(cb_id, "Chat no autorizado")
        except TelegramError:
            pass
        return
    parsed = parse_decision(cq.get("data") or "")
    if parsed is None:
        try:
            tg.answer_callback(cb_id, "Datos inválidos")
        except TelegramError:
            pass
        return
    decision, uuid = parsed
    if registry.resolve(uuid, decision):
        toast = "Aprobado" if decision == "APPROVED" else "Denegado"
        try:
            tg.answer_callback(cb_id, toast)
        except TelegramError:
            pass
        # handle_client thread edits the message; nothing else here.
        return
    try:
        tg.answer_callback(cb_id, "Ya no está pendiente")
    except TelegramError:
        pass
    audit.write({
        "event": "stale_callback",
        "uuid": uuid,
        "decision": decision,
    })


def get_peer_cred(sock: socket.socket) -> tuple[int, int, int]:
    fmt = "iII"
    size = struct.calcsize(fmt)
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    pid, uid, gid = struct.unpack(fmt, data)
    return pid, uid, gid


def parse_request(blob: bytes) -> dict:
    req: dict[str, str] = {}
    for line in blob.decode("utf-8", errors="replace").splitlines():
        line = line.rstrip()
        if not line or line == "END":
            continue
        if " " not in line:
            continue
        k, _, v = line.partition(" ")
        req[k] = v
    return req


def handle_client(
    conn: socket.socket,
    cfg: dict,
    tg: Telegram,
    registry: PendingRegistry,
    rate: RateLimiter,
    audit: AuditLog,
):
    try:
        conn.settimeout(cfg["timeout_s"] + 5)
        pid, uid, _gid = get_peer_cred(conn)
        try:
            username = pwd.getpwuid(uid).pw_name
        except KeyError:
            username = f"uid={uid}"

        chunks: list[bytes] = []
        while True:
            try:
                data = conn.recv(4096)
            except (socket.timeout, OSError):
                data = b""
            if not data:
                break
            chunks.append(data)
            if b"\nEND" in b"".join(chunks):
                break
        req = parse_request(b"".join(chunks))

        askpass_cmd = req.get("CMD", "")
        hostname = req.get("HOSTNAME", "?")
        cwd = req.get("PWD", "?")
        tty = req.get("TTY", "?")
        try:
            sudo_pid = int(req.get("PPID", "0"))
        except ValueError:
            sudo_pid = 0
        # Daemon runs as root, so it can read /proc/<sudo_pid>/cmdline even
        # though sudo set dumpable=0. Falls back to whatever askpass guessed.
        proc_cmd = read_proc_cmdline(sudo_pid) if sudo_pid > 0 else ""
        cmd = proc_cmd or askpass_cmd or "?"

        if cfg["allowed_users"] and username not in cfg["allowed_users"]:
            audit.write({
                "event": "denied_unauthorized_user",
                "peer_uid": uid, "peer_pid": pid, "peer_user": username,
                "cmd": cmd,
            })
            conn.sendall(b"DENIED\nuser not allowed\n")
            return

        if not rate.check_and_record():
            audit.write({
                "event": "denied_rate_limit",
                "peer_user": username, "cmd": cmd,
            })
            try:
                tg.send_message(
                    f"🚫 Rate limit excedido por <code>{html_escape(username)}</code>: "
                    f"<code>{html_escape(cmd)}</code>"
                )
            except TelegramError:
                pass
            conn.sendall(b"DENIED\nrate limited\n")
            return

        uuid = secrets.token_hex(4)
        deadline = time.time() + cfg["timeout_s"]
        ev = registry.register(uuid, deadline)

        prompt_body = (
            f"🔐 <b>Solicitud de sudo</b>\n"
            f"Usuario: <code>{html_escape(username)}</code>\n"
            f"Host: <code>{html_escape(hostname)}</code>\n"
            f"CWD: <code>{html_escape(cwd)}</code>\n"
            f"TTY: <code>{html_escape(tty)}</code>\n"
            f"Comando: <code>{html_escape(cmd)}</code>\n"
            f"PID: <code>{pid}</code>\n"
            f"ID: <code>{uuid}</code>"
        )
        prompt = (
            f"{prompt_body}\n\n"
            f"⏳ Esperando decisión ({cfg['timeout_s']}s)…"
        )

        audit.write({
            "event": "requested",
            "uuid": uuid, "peer_uid": uid, "peer_pid": pid, "peer_user": username,
            "cmd": cmd, "hostname": hostname, "cwd": cwd, "tty": tty,
            "dry_run": cfg["dry_run"],
        })

        try:
            message_id = tg.send_message(prompt, reply_markup=decision_keyboard(uuid))
        except TelegramError as e:
            registry.unregister(uuid)
            audit.write({
                "event": "denied_telegram_unreachable",
                "uuid": uuid, "error": str(e),
            })
            conn.sendall(b"DENIED\ntelegram unreachable (deny by default)\n")
            return

        ev.wait(timeout=cfg["timeout_s"] + 1)
        entry = registry.unregister(uuid)
        decision = entry["decision"] if entry else "TIMEOUT"
        if decision is None:
            decision = "TIMEOUT"

        audit.write({"event": "resolved", "uuid": uuid, "decision": decision})

        status_line = {
            "APPROVED": "✅ <b>Aprobado</b>",
            "DENIED": "❌ <b>Denegado</b>",
            "TIMEOUT": "⏱️ <b>Timeout</b>",
        }.get(decision, f"<b>{html_escape(decision)}</b>")
        try:
            tg.edit_message_text(
                message_id,
                f"{prompt_body}\n\n{status_line}",
                reply_markup={"inline_keyboard": []},
            )
        except TelegramError:
            pass

        if decision == "APPROVED":
            if cfg["dry_run"]:
                conn.sendall(b"APPROVED\nDRY_RUN_FAKE_PASSWORD\n")
            else:
                try:
                    pw = Path(cfg["password_path"]).read_text(encoding="utf-8").rstrip("\n")
                except OSError as e:
                    audit.write({
                        "event": "password_read_failed",
                        "uuid": uuid, "error": str(e),
                    })
                    conn.sendall(b"ERROR\npassword file unreadable\n")
                    return
                conn.sendall(("APPROVED\n" + pw + "\n").encode())
        elif decision == "DENIED":
            conn.sendall(b"DENIED\nuser denied\n")
        else:
            conn.sendall(b"DENIED\ntimeout\n")

    except Exception as e:
        audit.write({"event": "handler_exception", "error": repr(e)})
        try:
            conn.sendall(b"ERROR\ninternal error\n")
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(
    cfg: dict,
    tg: Telegram,
    registry: PendingRegistry,
    rate: RateLimiter,
    audit: AuditLog,
):
    sock_path = cfg["socket_path"]
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
    if Path(sock_path).exists():
        Path(sock_path).unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o660)
    if cfg.get("socket_group"):
        try:
            gid = grp.getgrnam(cfg["socket_group"]).gr_gid
            os.chown(sock_path, 0, gid)
        except KeyError:
            print(
                f"warn: group {cfg['socket_group']!r} not found; "
                f"socket left as root:root",
                file=sys.stderr,
            )
    srv.listen(8)
    print(f"listening on {sock_path}", file=sys.stderr)

    while True:
        conn, _addr = srv.accept()
        t = threading.Thread(
            target=handle_client,
            args=(conn, cfg, tg, registry, rate, audit),
            daemon=True,
        )
        t.start()


def main():
    cfg = load_config(CONFIG_PATH)
    tg = Telegram(cfg["approval_bot_token"], cfg["chat_id"])
    registry = PendingRegistry()
    rate = RateLimiter(cfg["rate_limit_n"], cfg["rate_limit_window_s"])
    audit = AuditLog(cfg["log_path"])

    reaper = MessageReaper(
        tg,
        cfg["chat_auto_delete_s"],
        cfg["chat_auto_delete_interval_s"],
        audit,
    )
    tg.reaper = reaper
    if reaper.enabled:
        threading.Thread(target=reaper.run, daemon=True, name="reaper").start()

    poll_thread = threading.Thread(
        target=telegram_poll_loop,
        args=(tg, registry, audit, cfg["poll_timeout_s"]),
        daemon=True,
    )
    poll_thread.start()

    audit.write({
        "event": "daemon_start",
        "dry_run": cfg["dry_run"],
        "chat_auto_delete_s": cfg["chat_auto_delete_s"],
    })
    try:
        serve(cfg, tg, registry, rate, audit)
    except KeyboardInterrupt:
        audit.write({"event": "daemon_stop"})


if __name__ == "__main__":
    main()
