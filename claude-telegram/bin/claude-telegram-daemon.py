#!/usr/bin/env python3
"""
claude-telegram daemon.

Long-polls a Telegram bot, treats every text message from the whitelisted
chat as input for `claude -p`, and posts the answer back. Maintains a single
persistent session via `--continue`. Reset with /new.

Runs as sergioc via systemd. Stdlib only (Python 3.11+ for tomllib).
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
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


CONFIG_PATH = os.environ.get("CTG_CONFIG", "/etc/claude-telegram/config.toml")

TELEGRAM_MAX = 4096
TELEGRAM_STREAM_LIMIT = 3800  # safety margin under 4096 for incremental edits
TELEGRAM_CAPTION_MAX = 1024

DOWNLOAD_DIR = Path.home() / ".cache" / "claude-telegram" / "downloads"
DOWNLOAD_MAX_AGE_S = 7 * 86400  # auto-gc files older than 7 days

STATE_DIR = Path.home() / ".local" / "state" / "claude-telegram"
STATE_FILE = STATE_DIR / "state.json"

# Telegram MarkdownV2 chars that must be backslash-escaped in prose.
MD2_SPECIAL = set("_*[]()~`>#+-=|{}.!\\")


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for key in ("bot_token", "chat_id"):
        if key not in cfg:
            print(f"config missing required key: {key}", file=sys.stderr)
            sys.exit(2)
    cfg.setdefault("claude_bin", "/home/sergioc/.local/bin/claude")
    cfg.setdefault("working_dir", "/home/sergioc")
    cfg.setdefault("extra_args", [])
    cfg.setdefault("timeout_s", 10800)
    cfg.setdefault("poll_timeout_s", 25)
    cfg.setdefault("typing_interval_s", 4)
    cfg.setdefault("heartbeat_interval_s", 300)
    cfg.setdefault("whisper_enabled", True)
    cfg.setdefault("whisper_model", "small")
    cfg.setdefault("whisper_device", "cpu")
    cfg.setdefault("whisper_compute_type", "int8")
    cfg.setdefault("whisper_language", "")  # "" = autodetect
    cfg.setdefault("response_mode", "summary")
    cfg.setdefault("streaming_enabled", True)
    cfg.setdefault("streaming_min_edit_s", 1.2)
    cfg.setdefault("summary_message_enabled", True)
    if cfg["response_mode"] not in ("summary", "stream"):
        print(
            f"config: response_mode={cfg['response_mode']!r} desconocido, usando 'summary'",
            file=sys.stderr,
        )
        cfg["response_mode"] = "summary"
    cfg.setdefault("output_attach_enabled", False)
    cfg.setdefault("output_attach_dir", "")  # "" = working_dir
    cfg.setdefault("output_attach_max_files", 5)
    cfg.setdefault("output_attach_max_bytes", 10 * 1024 * 1024)
    cfg.setdefault("output_attach_max_depth", 4)
    return cfg


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"state load failed: {e}", file=sys.stderr)
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"state save failed: {e}", file=sys.stderr)


def update_state(patch: dict) -> None:
    """Merge patch into current state and persist. Use this instead of
    save_state when you want to update one key (e.g. session_id) without
    nuking unrelated keys (e.g. model)."""
    state = load_state()
    state.update(patch)
    save_state(state)


def clear_state_keys(keys: list[str]) -> None:
    """Remove keys from state and persist. Used by /new to drop session_id
    while preserving the user's chosen model."""
    state = load_state()
    changed = False
    for k in keys:
        if k in state:
            del state[k]
            changed = True
    if changed:
        save_state(state)


MODEL_ALIASES = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# Inputs that mean "no override — use whatever claude defaults to".
MODEL_DEFAULT_TOKENS = {"default", "none", "-", ""}


def resolve_model(value: str) -> tuple[bool, str | None]:
    """Resolve a user-supplied model string. Returns (ok, resolved_id_or_None).

    - "default"/"none"/"-"/"" -> (True, None) (clear override)
    - alias in MODEL_ALIASES -> (True, full id)
    - raw id starting with "claude-" -> (True, value)
    - anything else -> (False, None)"""
    v = value.strip().lower()
    if v in MODEL_DEFAULT_TOKENS:
        return True, None
    if v in MODEL_ALIASES:
        return True, MODEL_ALIASES[v]
    # Accept full IDs case-insensitively but preserve the user's casing for
    # passthrough — claude IDs are lowercase in practice, but don't hardcode.
    if value.strip().startswith("claude-"):
        return True, value.strip()
    return False, None


def md2_escape_prose(s: str) -> str:
    out: list[str] = []
    for ch in s:
        if ch in MD2_SPECIAL:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def md2_escape_code(s: str) -> str:
    # Inside ``` fences: backslash and backtick are the only specials.
    return s.replace("\\", "\\\\").replace("`", "\\`")


def format_md2(text: str) -> str:
    """Format text for Telegram MarkdownV2.

    Splits on ``` fences. Even segments are prose (escape all MD2 specials);
    odd segments are code (wrap in ```lang and escape only \\ and `). If the
    last fence is unclosed we still wrap the trailing piece — the result stays
    valid even mid-stream."""
    parts = text.split("```")
    out: list[str] = []
    for i, p in enumerate(parts):
        if i % 2 == 0:
            out.append(md2_escape_prose(p))
        else:
            lang = ""
            body = p
            nl = p.find("\n")
            if 0 < nl <= 20:
                head = p[:nl]
                if head and all(c.isalnum() or c in "-_+" for c in head):
                    lang = head
                    body = p[nl + 1 :]
            out.append(f"```{lang}\n{md2_escape_code(body)}```")
    return "".join(out)


class TelegramError(Exception):
    pass


def _is_parse_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "can't parse" in msg or "parse_mode" in msg or "parse error" in msg


class Telegram:
    def __init__(self, token: str, chat_id: int):
        self._token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = int(chat_id)
        # Track message_ids we've sent or seen, so /clear can try to delete
        # them. Telegram only lets a bot delete messages <48h old in a private
        # chat. Capacity 2000 covers many days of normal chatter.
        self._tracked_ids: deque[int] = deque(maxlen=2000)
        self._tracked_lock = threading.Lock()

    def track(self, message_id: int) -> None:
        with self._tracked_lock:
            self._tracked_ids.append(message_id)

    def pop_tracked(self) -> list[int]:
        with self._tracked_lock:
            ids = list(self._tracked_ids)
            self._tracked_ids.clear()
            return ids

    def _post(self, method: str, payload: dict, timeout: int = 15) -> dict:
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

    def send_message(self, text: str, parse_mode: str | None = None) -> int:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            result = self._post("sendMessage", payload)
        except TelegramError as e:
            if parse_mode and _is_parse_error(e):
                payload.pop("parse_mode", None)
                result = self._post("sendMessage", payload)
            else:
                raise
        mid = result["message_id"]
        self.track(mid)
        return mid

    def edit_message(self, message_id: int, text: str, parse_mode: str | None = None) -> bool:
        """Edit a previously sent message. Returns True if changed, False if
        Telegram rejected as 'not modified' (text identical) — a normal
        no-op when streaming hasn't produced new chars yet."""
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            self._post("editMessageText", payload)
            return True
        except TelegramError as e:
            msg = str(e).lower()
            if "not modified" in msg:
                return False
            if parse_mode and _is_parse_error(e):
                payload.pop("parse_mode", None)
                try:
                    self._post("editMessageText", payload)
                    return True
                except TelegramError as e2:
                    if "not modified" in str(e2).lower():
                        return False
                    raise
            raise

    def send_document(self, file_path: Path, caption: str = "") -> int:
        """multipart/form-data upload of a single file."""
        boundary = f"----CTG{int(time.time() * 1000)}"
        chunks: list[bytes] = []

        def field(name: str, value: str) -> None:
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
            chunks.append(value.encode("utf-8"))
            chunks.append(b"\r\n")

        field("chat_id", str(self.chat_id))
        if caption:
            field("caption", caption[:TELEGRAM_CAPTION_MAX])
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="document"; filename="{file_path.name}"\r\n'.encode()
        )
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(file_path.read_bytes())
        chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        body = b"".join(chunks)

        req = urllib.request.Request(
            f"{self.base}/sendDocument",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(f"network error: {e}") from e
        if not result.get("ok"):
            raise TelegramError(f"api error: {result}")
        mid = result["result"]["message_id"]
        self.track(mid)
        return mid

    def delete_messages(self, ids: list[int]) -> tuple[int, int]:
        """Delete up to len(ids) messages in batches of 100. Returns
        (attempted, succeeded)."""
        attempted = 0
        succeeded = 0
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            attempted += len(batch)
            try:
                self._post(
                    "deleteMessages",
                    {"chat_id": self.chat_id, "message_ids": json.dumps(batch)},
                )
                succeeded += len(batch)
            except TelegramError as e:
                # Common cases: message older than 48h, or already deleted.
                # Fall back to per-message so partial success still counts.
                for mid in batch:
                    try:
                        self._post(
                            "deleteMessage",
                            {"chat_id": self.chat_id, "message_id": mid},
                        )
                        succeeded += 1
                    except TelegramError:
                        pass
                _ = e
        return attempted, succeeded

    def send_chat_action(self, action: str = "typing") -> None:
        try:
            self._post("sendChatAction", {"chat_id": self.chat_id, "action": action}, timeout=5)
        except TelegramError:
            pass

    def set_my_commands(self, commands: list[dict]) -> None:
        try:
            self._post("setMyCommands", {"commands": json.dumps(commands)})
        except TelegramError as e:
            print(f"setMyCommands failed: {e}", file=sys.stderr)

    def set_chat_menu_button(self) -> None:
        try:
            self._post(
                "setChatMenuButton",
                {"chat_id": self.chat_id, "menu_button": json.dumps({"type": "commands"})},
            )
        except TelegramError as e:
            print(f"setChatMenuButton failed: {e}", file=sys.stderr)

    def get_updates(self, offset: int, timeout: int) -> list:
        params = urllib.parse.urlencode({
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": json.dumps(["message"]),
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

    def download_file(self, file_id: str, dest_dir: Path, name_stem: str) -> Path:
        """Resolve file_id via getFile and download to dest_dir/<stem><ext>.

        Extension is taken from the Telegram-side file_path (or .bin)."""
        info = self._post("getFile", {"file_id": file_id})
        remote = info.get("file_path") or ""
        ext = Path(remote).suffix or ".bin"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{name_stem}{ext}"
        url = f"https://api.telegram.org/file/bot{self._token}/{remote}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                with open(dest, "wb") as f:
                    shutil.copyfileobj(resp, f)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(f"download failed: {e}") from e
        return dest


class ClaudeRunner:
    """Runs `claude -p` queries serially. One in-flight at a time.

    Two modes:
    - streaming (cfg['streaming_enabled']=true, default): uses
      --output-format stream-json --verbose, parses events line-by-line,
      pushes text to the optional on_chunk callback as it arrives, and
      captures session_id so restarts can resume the same conversation
      via -r <id> instead of the volatile --continue.
    - non-streaming: uses --output-format json, blocks until done.
      session_id is still captured from the JSON.
    """

    def __init__(
        self,
        cfg: dict,
        tg: Telegram,
        session_id: str | None = None,
        model: str | None = None,
    ):
        self.cfg = cfg
        self.tg = tg
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._session_id: str | None = session_id
        self._model: str | None = model

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def model(self) -> str | None:
        return self._model

    def set_model(self, model: str | None) -> None:
        """Set the active model override and persist. None = no override."""
        with self._lock:
            self._model = model
        if model is None:
            clear_state_keys(["model"])
        else:
            update_state({"model": model})

    def reset_session(self) -> bool:
        """Kill any in-flight claude and forget the conversation. Returns
        True if a process was killed. The model override is preserved — it's
        a user preference, not part of the thread."""
        killed = False
        with self._lock:
            self._session_id = None
            proc = self._proc
        clear_state_keys(["session_id"])
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
            killed = True
        return killed

    def cancel(self) -> bool:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except OSError:
                pass
            return True
        return False

    def run(
        self,
        prompt: str,
        on_chunk=None,
        on_tool=None,
        heartbeat: bool = True,
    ) -> tuple[int, str, str]:
        if self.cfg["streaming_enabled"]:
            return self._run_stream(prompt, on_chunk, on_tool, heartbeat=heartbeat)
        return self._run_blocking(prompt, heartbeat=heartbeat)

    def _build_cmd(self, prompt: str, stream: bool) -> list[str]:
        cmd = [self.cfg["claude_bin"], "-p"]
        if stream:
            cmd.extend(["--output-format", "stream-json", "--verbose"])
        else:
            cmd.extend(["--output-format", "json"])
        if self._session_id:
            cmd.extend(["-r", self._session_id])
        if self._model:
            cmd.extend(["--model", self._model])
        cmd.extend(self.cfg["extra_args"])
        cmd.append(prompt)
        return cmd

    def _spawn(self, cmd: list[str]) -> subprocess.Popen:
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        proc = subprocess.Popen(
            cmd,
            cwd=self.cfg["working_dir"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered (matters for streaming)
        )
        with self._lock:
            self._proc = proc
        return proc

    def _run_stream(
        self,
        prompt: str,
        on_chunk,
        on_tool,
        heartbeat: bool = True,
    ) -> tuple[int, str, str]:
        proc = self._spawn(self._build_cmd(prompt, stream=True))
        started_at = time.monotonic()

        text_parts: list[str] = []
        new_session_id: str | None = None
        first_chunk_seen = threading.Event()

        def reader() -> None:
            nonlocal new_session_id
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    print(f"claude (non-json): {line[:200]}", file=sys.stderr)
                    continue
                etype = evt.get("type")
                if etype == "system" and evt.get("subtype") == "init":
                    sid = evt.get("session_id")
                    if sid:
                        new_session_id = sid
                elif etype == "assistant":
                    blocks = (evt.get("message") or {}).get("content") or []
                    for blk in blocks:
                        btype = blk.get("type")
                        if btype == "text":
                            txt = blk.get("text", "")
                            if txt:
                                text_parts.append(txt)
                                first_chunk_seen.set()
                                if on_chunk:
                                    try:
                                        on_chunk(txt)
                                    except Exception as e:
                                        print(f"on_chunk: {e}", file=sys.stderr)
                        elif btype == "tool_use":
                            if on_tool:
                                try:
                                    on_tool(blk)
                                except Exception as e:
                                    print(f"on_tool: {e}", file=sys.stderr)
                elif etype == "result":
                    sid = evt.get("session_id")
                    if sid:
                        new_session_id = sid

        reader_t = threading.Thread(target=reader, daemon=True)
        reader_t.start()

        # Typing indicator until first chunk arrives, then heartbeat
        # only fires if there's a long silence between chunks.
        typing_stop = threading.Event()
        typing_t = threading.Thread(
            target=self._typing_until, args=(typing_stop, first_chunk_seen), daemon=True
        )
        typing_t.start()

        heartbeat_stop = threading.Event()
        if heartbeat:
            heartbeat_t = threading.Thread(
                target=self._heartbeat_loop, args=(heartbeat_stop, started_at), daemon=True
            )
            heartbeat_t.start()

        try:
            proc.wait(timeout=self.cfg["timeout_s"])
            rc = proc.returncode
            stderr = proc.stderr.read() if proc.stderr else ""
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            rc = -1
            stderr = (proc.stderr.read() if proc.stderr else "") + (
                "\n[claude-telegram] timeout, killed"
            )
        finally:
            typing_stop.set()
            heartbeat_stop.set()
            first_chunk_seen.set()  # unblock typing thread if still spinning
            reader_t.join(timeout=5)
            with self._lock:
                self._proc = None

        if rc == 0 and new_session_id:
            self._session_id = new_session_id
            update_state({"session_id": new_session_id})
        return rc, "".join(text_parts), stderr or ""

    def _run_blocking(self, prompt: str, heartbeat: bool = True) -> tuple[int, str, str]:
        proc = self._spawn(self._build_cmd(prompt, stream=False))
        started_at = time.monotonic()

        typing_stop = threading.Event()
        typing_t = threading.Thread(
            target=self._typing_loop, args=(typing_stop,), daemon=True
        )
        typing_t.start()

        heartbeat_stop = threading.Event()
        if heartbeat:
            heartbeat_t = threading.Thread(
                target=self._heartbeat_loop, args=(heartbeat_stop, started_at), daemon=True
            )
            heartbeat_t.start()

        try:
            stdout, stderr = proc.communicate(timeout=self.cfg["timeout_s"])
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            rc = -1
            stderr = (stderr or "") + "\n[claude-telegram] timeout, killed"
        finally:
            typing_stop.set()
            heartbeat_stop.set()
            with self._lock:
                self._proc = None

        text = ""
        new_session_id: str | None = None
        if rc == 0 and stdout:
            try:
                obj = json.loads(stdout)
                text = obj.get("result") or ""
                new_session_id = obj.get("session_id") or None
            except json.JSONDecodeError:
                text = stdout
        if rc == 0 and new_session_id:
            self._session_id = new_session_id
            update_state({"session_id": new_session_id})
        return rc, text, stderr or ""

    def _typing_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.tg.send_chat_action("typing")
            if stop.wait(self.cfg["typing_interval_s"]):
                return

    def _typing_until(self, stop: threading.Event, until: threading.Event) -> None:
        while not stop.is_set() and not until.is_set():
            self.tg.send_chat_action("typing")
            if stop.wait(self.cfg["typing_interval_s"]):
                return
            if until.is_set():
                return

    def _heartbeat_loop(self, stop: threading.Event, started_at: float) -> None:
        interval = self.cfg["heartbeat_interval_s"]
        if interval <= 0:
            return
        while True:
            if stop.wait(interval):
                return
            mins = int((time.monotonic() - started_at) // 60)
            try:
                self.tg.send_message(f"⏳ claude sigue corriendo ({mins} min)")
            except TelegramError:
                pass


def _format_tool_use(block: dict) -> str:
    """Render a tool_use event as a one-line indicator for the chat."""
    name = block.get("name") or ""
    inp = block.get("input") or {}
    icon_by_name = {
        "Bash": "🔧",
        "Read": "📖",
        "Edit": "✏️",
        "Write": "📝",
        "Grep": "🔎",
        "Glob": "🔎",
        "WebFetch": "🌐",
        "WebSearch": "🌐",
        "Task": "🤖",
        "TodoWrite": "📋",
    }
    icon = icon_by_name.get(name, "🛠️")
    detail = ""
    if name == "Bash":
        detail = (inp.get("command") or "").splitlines()[0] if inp.get("command") else ""
    elif name in ("Read", "Edit", "Write"):
        detail = inp.get("file_path") or ""
    elif name in ("Grep", "Glob"):
        detail = inp.get("pattern") or inp.get("path") or ""
    elif name == "WebFetch":
        detail = inp.get("url") or ""
    elif name == "WebSearch":
        detail = inp.get("query") or ""
    if detail and len(detail) > 120:
        detail = detail[:117] + "…"
    return f"{icon} {name}: {detail}".rstrip(": ").rstrip()


_TOOL_CATEGORIES = {
    "Read": "read",
    "Edit": "edit",
    "Write": "edit",
    "Bash": "bash",
    "Grep": "search",
    "Glob": "search",
    "WebFetch": "web",
    "WebSearch": "web",
    "Task": "subagent",
    "TodoWrite": "todo",
}


def _tool_category(block: dict) -> str:
    name = block.get("name") or ""
    return _TOOL_CATEGORIES.get(name, "other")


def _format_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if m else f"{h}h"


class StreamingMessage:
    """Owns one or more Telegram messages that get edited as text streams in.

    Edits are throttled (Telegram floods at >1/sec to a chat). When the
    current message overflows TELEGRAM_STREAM_LIMIT we leave it as-is and
    start a new one. Always rendered with MarkdownV2 (escaped via format_md2);
    on parse failure the Telegram helper retries plain text."""

    def __init__(self, tg: Telegram, min_edit_interval: float):
        self.tg = tg
        self.min_edit_interval = min_edit_interval
        self._mid: int | None = None
        self._displayed = ""        # what's currently shown in self._mid (raw text)
        self._pending = ""          # buffered text not yet rendered
        self._last_render = 0.0
        self._lock = threading.Lock()
        self.had_tool_events = False  # set when tool_event() is called at least once
        self.message_count = 0        # how many Telegram messages this stream spans

    def append(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            self._pending += delta
            self._render(force=False)

    def tool_event(self, block: dict) -> None:
        """Inline a one-line tool indicator into the running message."""
        line = _format_tool_use(block)
        if not line:
            return
        self.had_tool_events = True
        prefix = "" if (self._displayed.endswith("\n") or not self._displayed) else "\n"
        self.append(f"{prefix}_{line}_\n")

    def finalize(self) -> None:
        with self._lock:
            self._render(force=True)

    def _render(self, force: bool) -> None:
        if not self._pending:
            return
        now = time.monotonic()
        if not force and (now - self._last_render) < self.min_edit_interval:
            return
        while self._pending:
            if self._mid is None:
                room = TELEGRAM_STREAM_LIMIT
            else:
                room = TELEGRAM_STREAM_LIMIT - len(self._displayed)
            if room <= 0:
                # current message full — leave it, start a new one
                self._mid = None
                self._displayed = ""
                continue
            chunk = self._pending[:room]
            self._pending = self._pending[len(chunk):]
            self._displayed += chunk
            try:
                formatted = format_md2(self._displayed)
                if self._mid is None:
                    self._mid = self.tg.send_message(formatted, parse_mode="MarkdownV2")
                    self.message_count += 1
                else:
                    self.tg.edit_message(self._mid, formatted, parse_mode="MarkdownV2")
            except TelegramError as e:
                print(f"streaming render: {e}", file=sys.stderr)
        self._last_render = now


class SummaryCollector:
    """Accumulates Claude's text and tool usage during a turn without
    posting anything live. The caller renders a single consolidated message
    at the end via build_summary_message()."""

    LABELS = {
        "read":     ("leído", "leídos"),
        "edit":     ("editado", "editados"),
        "bash":     ("comando", "comandos"),
        "search":   ("búsqueda", "búsquedas"),
        "web":      ("web", "web"),
        "subagent": ("subagent", "subagents"),
        "todo":     ("todo", "todos"),
        "other":    ("acción", "acciones"),
    }
    ORDER = ["read", "edit", "bash", "search", "web", "subagent", "todo", "other"]

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.counts: dict[str, int] = {}

    def append(self, delta: str) -> None:
        if delta:
            self.text_parts.append(delta)

    def tool_event(self, block: dict) -> None:
        cat = _tool_category(block)
        self.counts[cat] = self.counts.get(cat, 0) + 1

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()

    def summary_line(self) -> str:
        parts: list[str] = []
        for cat in self.ORDER:
            n = self.counts.get(cat, 0)
            if not n:
                continue
            sing, plur = self.LABELS[cat]
            parts.append(f"{n} {sing if n == 1 else plur}")
        return " · ".join(parts)


def build_summary_message(
    rc: int,
    collector: SummaryCollector,
    err: str,
    elapsed: float,
) -> str:
    duration = _format_duration(elapsed)
    if rc == 0:
        header = f"✅ Listo · {duration}"
    elif rc == -1:
        header = f"⏱️ Timeout · {duration}"
    else:
        header = f"⚠️ Error (exit={rc}) · {duration}"

    lines: list[str] = [header]
    summary = collector.summary_line()
    if summary:
        lines.append(f"📋 {summary}")

    body = collector.text
    if rc != 0 and err.strip():
        tail = err.strip()[-1500:]
        body = f"{body}\n\n{tail}" if body else tail
    if rc == 0 and not body:
        body = "(claude no devolvió output)"

    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines)


class OutputWatcher:
    """Snapshots a directory tree pre-run, reports files modified after a
    given epoch when asked. Used to auto-attach files Claude generates."""

    EXCLUDE_DIR_NAMES = {
        ".git", ".cache", ".venv", "venv", "node_modules",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".tox", ".idea", ".vscode", "target", "dist", "build",
    }

    def __init__(
        self,
        root: Path,
        max_files: int,
        max_bytes_per_file: int,
        max_depth: int,
    ):
        self.root = root
        self.max_files = max_files
        self.max_bytes_per_file = max_bytes_per_file
        self.max_depth = max_depth

    def collect_new(self, since: float) -> list[Path]:
        if not self.root.exists() or not self.root.is_dir():
            return []
        results: list[Path] = []
        self._walk(self.root, since, depth=0, out=results)
        results.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return results[: self.max_files]

    def _walk(self, dir_path: Path, since: float, depth: int, out: list[Path]) -> None:
        if depth > self.max_depth:
            return
        try:
            entries = list(dir_path.iterdir())
        except OSError:
            return
        for p in entries:
            name = p.name
            if name.startswith("."):
                continue
            try:
                if p.is_dir():
                    if name in self.EXCLUDE_DIR_NAMES:
                        continue
                    self._walk(p, since, depth + 1, out)
                elif p.is_file():
                    st = p.stat()
                    if st.st_mtime < since:
                        continue
                    if st.st_size == 0 or st.st_size > self.max_bytes_per_file:
                        continue
                    out.append(p)
            except OSError:
                continue


class Transcriber:
    """Lazy-loaded faster-whisper wrapper. Model loads on first use and stays
    resident."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise RuntimeError(
                    "faster-whisper no está instalado en el python del daemon"
                ) from e
            print(
                f"transcriber: loading model={self.cfg['whisper_model']} "
                f"device={self.cfg['whisper_device']} "
                f"compute={self.cfg['whisper_compute_type']}",
                file=sys.stderr,
            )
            self._model = WhisperModel(
                self.cfg["whisper_model"],
                device=self.cfg["whisper_device"],
                compute_type=self.cfg["whisper_compute_type"],
            )
            return self._model

    def transcribe(self, path: Path) -> str:
        model = self._load()
        lang = self.cfg.get("whisper_language") or None
        segments, _info = model.transcribe(
            str(path),
            language=lang,
            vad_filter=True,
        )
        return " ".join(s.text.strip() for s in segments).strip()


def gc_old_downloads(dest_dir: Path, max_age_s: int = DOWNLOAD_MAX_AGE_S) -> None:
    if not dest_dir.exists():
        return
    now = time.time()
    for p in dest_dir.iterdir():
        try:
            if p.is_file() and now - p.stat().st_mtime > max_age_s:
                p.unlink()
        except OSError:
            pass


def extract_image_file_id(msg: dict) -> str | None:
    """Return the file_id of an image attached to msg, or None.

    Picks the largest size for `photo`. For `document`, only accepts
    image/* mime types."""
    photos = msg.get("photo")
    if photos:
        largest = max(photos, key=lambda p: p.get("file_size") or 0)
        return largest.get("file_id")
    document = msg.get("document")
    if document and (document.get("mime_type") or "").startswith("image/"):
        return document.get("file_id")
    return None


def extract_voice_file_id(msg: dict) -> tuple[str | None, str]:
    """Return (file_id, kind) for an audio attachment, or (None, "").

    Accepts `voice` (Opus/OGG voice notes), `audio` (music files), and
    `document` with mime_type audio/*."""
    voice = msg.get("voice")
    if voice:
        return voice.get("file_id"), "voice"
    audio = msg.get("audio")
    if audio:
        return audio.get("file_id"), "audio"
    document = msg.get("document")
    if document and (document.get("mime_type") or "").startswith("audio/"):
        return document.get("file_id"), "document"
    return None, ""


def extract_document_file_id(msg: dict) -> tuple[str | None, str, str]:
    """Return (file_id, filename, mime) for a generic document attachment.

    Excludes image/* and audio/* mimes — those are picked up by
    extract_image_file_id and extract_voice_file_id respectively. Anything
    else (PDF, PPTX, ZIP, .pkg, text, code, etc.) is treated as a generic
    document and handed to claude as a path."""
    document = msg.get("document")
    if not document:
        return None, "", ""
    mime = document.get("mime_type") or ""
    if mime.startswith("image/") or mime.startswith("audio/"):
        return None, "", ""
    return document.get("file_id"), document.get("file_name") or "", mime


def build_image_prompt(caption: str, image_path: Path) -> str:
    caption = (caption or "").strip()
    tag = f"[Imagen adjunta: {image_path}]"
    if caption:
        return f"{caption}\n\n{tag}"
    return f"{tag}\n\n(El mensaje no traía caption — abrila con la herramienta Read y respondé según lo que muestre.)"


def build_document_prompt(
    caption: str, doc_path: Path, original_name: str, mime: str
) -> str:
    caption = (caption or "").strip()
    name_hint = f", nombre original: {original_name}" if original_name else ""
    mime_hint = f", mime: {mime}" if mime else ""
    tag = f"[Archivo adjunto: {doc_path}{name_hint}{mime_hint}]"
    if caption:
        return f"{caption}\n\n{tag}"
    return (
        f"{tag}\n\n"
        "(El mensaje no traía caption — inspeccioná el archivo y respondé "
        "según lo que encuentres. Para PDF/texto/código usá Read directamente; "
        "para zip/pkg/office usá Bash con la herramienta apropiada "
        "(unzip, pdftotext, pandoc, etc.) si hace falta.)"
    )


def chunk_text(text: str, limit: int = TELEGRAM_MAX) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        # try to break at a newline near the limit
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def safe_send(tg: Telegram, text: str) -> None:
    for chunk in chunk_text(text):
        try:
            tg.send_message(chunk)
        except TelegramError as e:
            print(f"send failed: {e}", file=sys.stderr)


def safe_send_md2(tg: Telegram, text: str) -> None:
    """Like safe_send but renders as MarkdownV2. Chunks at a tighter limit
    to leave headroom for escaped chars; send_message falls back to plain
    text if the formatted body still trips the parser."""
    for chunk in chunk_text(text, limit=TELEGRAM_STREAM_LIMIT):
        try:
            tg.send_message(format_md2(chunk), parse_mode="MarkdownV2")
        except TelegramError as e:
            print(f"send md2 failed: {e}", file=sys.stderr)


CONTROL_COMMANDS = frozenset({
    "/ping", "/help", "/start", "/new", "/stop",
    "/cwd", "/session", "/clear", "/clear_chat", "/model",
})


def _command_name(text: str) -> str:
    """Return the command word (e.g. "/stop") with @botname stripped, or ""."""
    if not text.startswith("/"):
        return ""
    head = text.split(maxsplit=1)[0]
    return head.split("@")[0]


def handle_slash_command(text: str, tg: Telegram, runner: ClaudeRunner) -> bool:
    """Run a control slash command. Returns True if handled, False if unknown.

    Designed to run *outside* the worker_lock so /stop and /new can preempt
    an in-progress claude run — gating them on the worker would queue them
    behind the very query they're trying to interrupt."""
    cmd = _command_name(text)
    if cmd == "/ping":
        safe_send(tg, "pong")
        return True
    if cmd in ("/help", "/start"):
        safe_send(tg, (
            "Comandos:\n"
            "/new   — empieza una sesión nueva (olvida el contexto)\n"
            "/stop  — cancela la query en curso\n"
            "/clear — borra los mensajes del chat (los <48h que el bot conoce)\n"
            "/cwd   — muestra el working directory\n"
            "/session — muestra el session_id actual\n"
            "/model — muestra/cambia el modelo (opus/sonnet/haiku/default)\n"
            "/ping  — health check\n"
            "/help  — esta ayuda\n\n"
            "Cualquier otro texto se envía a `claude -p`.\n"
            "Fotos / imágenes-como-documento: se descargan y claude las "
            "abre con Read.\n"
            "Voice notes / audios: se transcriben local con faster-whisper "
            "y la transcripción va a claude.\n"
            "Documentos (PDF, PPT, ZIP, código, etc.): se descargan y se le "
            "pasa el path a claude para que los inspeccione.\n"
            "Si claude genera archivos en el working dir y "
            "output_attach_enabled=true, te los manda como adjuntos."
        ))
        return True
    if cmd == "/new":
        killed = runner.reset_session()
        extra = " (cancelé la query en curso)" if killed else ""
        safe_send(tg, f"🆕 Sesión reseteada{extra}. El próximo mensaje empieza desde cero.")
        return True
    if cmd == "/stop":
        if runner.cancel():
            safe_send(tg, "🛑 Cancelando query en curso…")
        else:
            safe_send(tg, "(no había nada corriendo)")
        return True
    if cmd == "/cwd":
        safe_send(tg, f"cwd: {runner.cfg['working_dir']}")
        return True
    if cmd == "/session":
        sid = runner.session_id
        safe_send(tg, f"session_id: {sid}" if sid else "(sin sesión guardada — el próximo mensaje empieza una nueva)")
        return True
    if cmd == "/model":
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            current = runner.model or "default (el que use claude por defecto)"
            alias_lines = "\n".join(f"  • {a} → {mid}" for a, mid in MODEL_ALIASES.items())
            safe_send(tg, (
                f"Modelo actual: {current}\n\n"
                f"Para cambiar:\n"
                f"  /model opus\n"
                f"  /model sonnet\n"
                f"  /model haiku\n"
                f"  /model default   (sin override)\n"
                f"  /model claude-…  (id completo)\n\n"
                f"Alias:\n{alias_lines}"
            ))
            return True
        ok, resolved = resolve_model(parts[1])
        if not ok:
            opts = ", ".join(list(MODEL_ALIASES) + ["default"])
            safe_send(tg, f"⚠️ Modelo desconocido: {parts[1]}\nOpciones: {opts}, o un id que empiece con 'claude-'")
            return True
        runner.set_model(resolved)
        if resolved is None:
            safe_send(tg, "✅ Modelo: default (sin override)")
        else:
            safe_send(tg, f"✅ Modelo: {resolved}")
        return True
    if cmd in ("/clear", "/clear_chat"):
        ids = tg.pop_tracked()
        if not ids:
            safe_send(tg, "(no hay mensajes tracked para borrar)")
            return True
        attempted, succeeded = tg.delete_messages(ids)
        failed = attempted - succeeded
        note = f" ({failed} no se pudieron borrar — probablemente >48h)" if failed else ""
        safe_send(tg, f"🧹 Borré {succeeded}/{attempted} mensajes{note}")
        return True
    return False


def handle_text(
    text: str,
    tg: Telegram,
    runner: ClaudeRunner,
    watcher: OutputWatcher | None,
) -> None:
    text = text.strip()
    if not text:
        return

    if _command_name(text) in CONTROL_COMMANDS:
        # Should normally be intercepted by poll_loop, but handle here too
        # so a direct call still does the right thing.
        handle_slash_command(text, tg, runner)
        return

    started_at = time.time()
    started_mono = time.monotonic()
    response_mode = runner.cfg["response_mode"]

    if response_mode == "summary":
        collector = SummaryCollector()
        rc, out, err = runner.run(
            text,
            on_chunk=collector.append,
            on_tool=collector.tool_event,
            heartbeat=False,
        )
        elapsed = time.monotonic() - started_mono
        safe_send(tg, build_summary_message(rc, collector, err, elapsed))
    else:
        streaming = bool(runner.cfg["streaming_enabled"])
        stream_msg = StreamingMessage(tg, runner.cfg["streaming_min_edit_s"]) if streaming else None
        on_chunk = stream_msg.append if stream_msg else None
        on_tool = stream_msg.tool_event if stream_msg else None

        rc, out, err = runner.run(text, on_chunk=on_chunk, on_tool=on_tool)

        if stream_msg:
            stream_msg.finalize()
        elif out.strip():
            safe_send(tg, out)

        if rc != 0:
            msg = f"⚠️ claude exit={rc}"
            if err.strip():
                msg += f"\n{err.strip()[-2000:]}"
            safe_send(tg, msg)
        elif not out.strip():
            safe_send(tg, "(claude no devolvió output)")
        elif (
            stream_msg
            and runner.cfg["summary_message_enabled"]
            and stream_msg.had_tool_events
            and stream_msg.message_count > 1
        ):
            # Only resend a clean summary when the stream both used tools AND
            # spilled into multiple Telegram messages — i.e. the answer ended up
            # scrolled above the activity log. For single-message answers the
            # streamed text already shows the response at the bottom and a
            # summary would be a near-literal duplicate.
            safe_send_md2(tg, out)

    if watcher and rc == 0:
        try:
            new_files = watcher.collect_new(started_at)
        except Exception as e:
            print(f"watcher: {e}", file=sys.stderr)
            new_files = []
        for f in new_files:
            try:
                rel = f.relative_to(watcher.root)
            except ValueError:
                rel = f.name
            try:
                tg.send_document(f, caption=f"📎 {rel}")
            except TelegramError as e:
                print(f"send_document {f}: {e}", file=sys.stderr)


def poll_loop(
    tg: Telegram,
    runner: ClaudeRunner,
    transcriber: Transcriber,
    watcher: OutputWatcher | None,
    cfg: dict,
    poll_timeout: int,
) -> None:
    offset = 0
    try:
        old = tg.get_updates(offset=-1, timeout=0)
        if old:
            offset = old[-1]["update_id"] + 1
    except TelegramError as e:
        print(f"poll: initial drain failed: {e}", file=sys.stderr)

    worker_lock = threading.Lock()

    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout=poll_timeout)
        except TelegramError as e:
            print(f"poll: {e}", file=sys.stderr)
            time.sleep(2)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue
            if msg.get("chat", {}).get("id") != tg.chat_id:
                continue
            mid = msg.get("message_id")
            if mid is not None:
                tg.track(mid)

            text = msg.get("text") or ""
            caption = msg.get("caption") or ""
            image_file_id = extract_image_file_id(msg)
            voice_file_id, voice_kind = extract_voice_file_id(msg)
            doc_file_id, doc_name, doc_mime = extract_document_file_id(msg)

            prompt: str | None = None
            if voice_file_id:
                if not cfg["whisper_enabled"]:
                    try:
                        tg.send_message("⚠️ transcripción desactivada (whisper_enabled=false)")
                    except TelegramError:
                        pass
                    continue
                gc_old_downloads(DOWNLOAD_DIR)
                stem = f"voice_{time.strftime('%Y%m%d_%H%M%S')}_{mid}"
                try:
                    audio_path = tg.download_file(voice_file_id, DOWNLOAD_DIR, stem)
                except TelegramError as e:
                    print(f"download failed: {e}", file=sys.stderr)
                    try:
                        tg.send_message(f"⚠️ no pude bajar el audio: {e}")
                    except TelegramError:
                        pass
                    continue
                tg.send_chat_action("typing")
                try:
                    transcript = transcriber.transcribe(audio_path)
                except Exception as e:
                    print(f"transcribe failed: {e}", file=sys.stderr)
                    try:
                        tg.send_message(f"⚠️ transcripción falló: {e}")
                    except TelegramError:
                        pass
                    continue
                if not transcript:
                    try:
                        tg.send_message("🎙️ (audio sin texto detectable)")
                    except TelegramError:
                        pass
                    continue
                try:
                    tg.send_message(f"🎙️ {transcript}")
                except TelegramError:
                    pass
                if caption.strip():
                    prompt = f"{caption.strip()}\n\n{transcript}"
                else:
                    prompt = transcript
            elif image_file_id:
                gc_old_downloads(DOWNLOAD_DIR)
                stem = f"img_{time.strftime('%Y%m%d_%H%M%S')}_{mid}"
                try:
                    image_path = tg.download_file(image_file_id, DOWNLOAD_DIR, stem)
                except TelegramError as e:
                    print(f"download failed: {e}", file=sys.stderr)
                    try:
                        tg.send_message(f"⚠️ no pude bajar la imagen: {e}")
                    except TelegramError:
                        pass
                    continue
                prompt = build_image_prompt(caption, image_path)
            elif doc_file_id:
                gc_old_downloads(DOWNLOAD_DIR)
                stem = f"doc_{time.strftime('%Y%m%d_%H%M%S')}_{mid}"
                try:
                    doc_path = tg.download_file(doc_file_id, DOWNLOAD_DIR, stem)
                except TelegramError as e:
                    print(f"download failed: {e}", file=sys.stderr)
                    try:
                        tg.send_message(f"⚠️ no pude bajar el archivo: {e}")
                    except TelegramError:
                        pass
                    continue
                prompt = build_document_prompt(caption, doc_path, doc_name, doc_mime)
            elif text:
                prompt = text

            if not prompt:
                continue

            # Slash commands run *outside* the worker_lock so /stop and /new
            # can actually preempt an in-progress claude run; otherwise they
            # would queue behind the very query they're meant to interrupt.
            if _command_name(prompt) in CONTROL_COMMANDS:
                def cmd_work(p=prompt):
                    try:
                        handle_slash_command(p, tg, runner)
                    except Exception as e:
                        try:
                            tg.send_message(f"⚠️ command error: {e!r}")
                        except TelegramError:
                            pass

                threading.Thread(target=cmd_work, daemon=True).start()
                continue

            # serialise: one message at a time
            def work(p=prompt):
                with worker_lock:
                    try:
                        handle_text(p, tg, runner, watcher)
                    except Exception as e:
                        try:
                            tg.send_message(f"⚠️ handler error: {e!r}")
                        except TelegramError:
                            pass

            threading.Thread(target=work, daemon=True).start()


def main() -> None:
    cfg = load_config(CONFIG_PATH)

    if not Path(cfg["claude_bin"]).exists():
        print(f"claude binary not found: {cfg['claude_bin']}", file=sys.stderr)
        sys.exit(2)

    state = load_state()
    saved_sid = state.get("session_id")
    saved_model = state.get("model")

    tg = Telegram(cfg["bot_token"], cfg["chat_id"])
    runner = ClaudeRunner(cfg, tg, session_id=saved_sid, model=saved_model)
    transcriber = Transcriber(cfg)

    watcher: OutputWatcher | None = None
    if cfg["output_attach_enabled"]:
        watch_root = Path(cfg["output_attach_dir"] or cfg["working_dir"]).expanduser()
        watcher = OutputWatcher(
            watch_root,
            max_files=int(cfg["output_attach_max_files"]),
            max_bytes_per_file=int(cfg["output_attach_max_bytes"]),
            max_depth=int(cfg["output_attach_max_depth"]),
        )

    tg.set_my_commands([
        {"command": "new", "description": "Sesión nueva (borra contexto)"},
        {"command": "stop", "description": "Cancelar query en curso"},
        {"command": "clear", "description": "Borrar mensajes del chat (<48h)"},
        {"command": "cwd", "description": "Mostrar working directory"},
        {"command": "session", "description": "Mostrar session_id actual"},
        {"command": "model", "description": "Mostrar/cambiar modelo (opus/sonnet/haiku/default)"},
        {"command": "ping", "description": "Health check"},
        {"command": "help", "description": "Ayuda"},
    ])
    tg.set_chat_menu_button()

    print(
        f"claude-telegram up: bin={cfg['claude_bin']} cwd={cfg['working_dir']} "
        f"extra={shlex.join(cfg['extra_args'])} "
        f"response_mode={cfg['response_mode']} "
        f"streaming={cfg['streaming_enabled']} "
        f"resume_sid={saved_sid or '(none)'} "
        f"model={saved_model or '(default)'} "
        f"attach={cfg['output_attach_enabled']}",
        file=sys.stderr,
    )

    try:
        poll_loop(tg, runner, transcriber, watcher, cfg, cfg["poll_timeout_s"])
    except KeyboardInterrupt:
        print("shutting down", file=sys.stderr)


if __name__ == "__main__":
    main()
