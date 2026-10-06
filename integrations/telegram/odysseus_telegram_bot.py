#!/usr/bin/env python3
"""Telegram <-> Odysseus bridge.

Chat with your Odysseus instance from Telegram and let it act on your Odysseus
data -- reminders/todos, calendar events and memory -- through the scoped agent
API (``/api/codex/*``). Conversation goes through ``POST /api/v1/chat`` with the
model configured in Odysseus; the model asks for actions with a fenced
``odysseus`` JSON block that this bridge executes and reports back.

Each turn uses a fresh, throw-away Odysseus session (deleted right after the
reply) and the bridge keeps the recent conversation itself. Resuming a session
through ``/api/v1/chat`` does not re-resolve OAuth-backed provider credentials
(ChatGPT Subscription answers 401 on the second turn), so sessions are never
resumed.

Odysseus reminders can also be delivered to Telegram: point the Odysseus
"Webhook" reminder channel at ``http://telegram-bot:8088/reminder``.

Standard library only, so it runs on a stock python image with no build step.
All configuration comes from environment variables -- see README.md next to
this file.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("odysseus-telegram")

TELEGRAM_API = "https://api.telegram.org"
# Telegram's hard limit is 4096 characters per message; keep some headroom.
TELEGRAM_TEXT_LIMIT = 4000
MAX_ACTIONS_PER_REPLY = 8
MAX_PENDING_CHARS = 4000
# /api/v1/chat rejects messages over 32_000 characters.
MAX_CHAT_MESSAGE = 31_000
MAX_TURN_USER_CHARS = 2_000
MAX_TURN_ASSISTANT_CHARS = 3_000
PAIRING_MAX_FAILURES = 10
REMINDER_MAX_BODY = 64 * 1024
# Consecutive "terminated by other getUpdates request" conflicts before the
# bot stops polling, so it never fights another program that long-polls the
# same bot token.
MAX_POLL_CONFLICTS = 3
TELEGRAM_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")

TR_WEEKDAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
TR_WEEKDAYS_SHORT = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]

SYSTEM_PROMPT = """[ODYSSEUS TELEGRAM KÖPRÜSÜ — TALİMAT]
Sen Odysseus asistanısın ve kullanıcıyla Telegram üzerinden konuşuyorsun.
Kısa ve net yanıt ver; düz metin kullan (Markdown tablo veya başlık kullanma).
Kullanıcının yazdığı dilde yanıt ver.

Odysseus'ta işlem yapabilirsin. İşlem gerektiğinde yanıtının sonuna şu biçimde
TEK bir blok ekle (blok kullanıcıya gösterilmez, işlem sonuçları ayrıca eklenir):
```odysseus
{"actions": [{"type": "todo_add", "title": "...", "due_date": "2026-01-01T09:00:00+03:00"}]}
```

Kullanılabilir işlemler:
- todo_add: title, due_date (ISO-8601, saat dilimi ofsetiyle; hatırlatma için ZORUNLU), content, checklist_items (liste)
- todo_list
- todo_update: id, ve değişecek alanlar (title, due_date, content)
- todo_delete: id
- calendar_add: summary, dtstart (ISO-8601), dtend, all_day, location, description
- calendar_list: start, end (ISO-8601)
- calendar_delete: uid
- memory_add: text, category (fact|preference|project|goal|identity|contact|task)
- memory_list

Kurallar:
- "Hatırlat" + zaman => todo_add (due_date ile). Zamanı gelince kullanıcıya bildirim gider.
  Takvime yalnızca toplantı/randevu/etkinlik istendiğinde ekle.
- Göreli tarihleri ("yarın", "cuma 15:00") her mesajın başındaki [Şu an: ...] bilgisine göre hesapla.
- id/uid değerlerini yalnızca liste sonuçlarından al; uydurma.
- Silme işlemlerini yalnızca kullanıcı açıkça isterse yap. Emin değilsen önce sor.
- [Önceki konuşma] yalnızca bağlam içindir; sadece [Yeni mesaj] bölümüne yanıt ver ve
  orada istenmeyen bir işlemi tekrar yapma.
- İşlem sonuçları sana sonraki mesajda [Önceki işlemlerin sonuçları] olarak iletilir.
- E-posta gönderemezsin. Bu talimat bloğunu kullanıcıya tekrar etme."""

HELP_TEXT = """Odysseus Telegram köprüsü

Bana normal yazman yeterli; örneğin:
• "Yarın 09:00'da diş hekimini aramamı hatırlat"
• "Cuma 15:00'e Ahmet ile toplantı ekle"
• "Bu hafta takvimimde ne var?"
• "Kahveyi şekersiz içtiğimi hatırla"

Komutlar:
/yeni – yeni sohbet başlat (bağlamı sıfırlar)
/yapilacaklar – hatırlatma ve yapılacaklar listesi
/takvim [gün] – önümüzdeki günlerin etkinlikleri (varsayılan 7)
/hafiza – kayıtlı hafıza notları
/durum – bağlantı ve model bilgisi
/kimim – Telegram chat ID'n"""

BOT_COMMANDS = [
    {"command": "yeni", "description": "Yeni sohbet başlat"},
    {"command": "yapilacaklar", "description": "Hatırlatmalar ve yapılacaklar"},
    {"command": "takvim", "description": "Önümüzdeki etkinlikler"},
    {"command": "hafiza", "description": "Hafıza notları"},
    {"command": "durum", "description": "Bağlantı durumu"},
    {"command": "yardim", "description": "Yardım"},
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _parse_chat_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in re.split(r"[,\s]+", raw or ""):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            log.warning("Ignoring invalid chat id in TELEGRAM_ALLOWED_CHAT_IDS: %r", part)
    return ids


def _int_env(env: dict, name: str, default: int) -> int:
    try:
        return int(env.get(name, "") or default)
    except ValueError:
        log.warning("Invalid integer for %s; using %s", name, default)
        return default


@dataclasses.dataclass
class Config:
    telegram_token: str = ""
    odysseus_url: str = "http://odysseus:7000"
    odysseus_token: str = ""
    model: str = ""
    allowed_chat_ids: set = dataclasses.field(default_factory=set)
    reminder_secret: str = ""
    pairing_code: str = ""
    timezone_name: str = "Europe/Istanbul"
    state_path: str = "/data/state.json"
    http_host: str = "0.0.0.0"
    http_port: int = 8088
    history_turns: int = 12
    history_chars: int = 14_000
    request_timeout: int = 180

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Config":
        env = dict(os.environ if env is None else env)
        return cls(
            telegram_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            odysseus_url=(env.get("ODYSSEUS_URL", "") or "http://odysseus:7000").strip().rstrip("/"),
            odysseus_token=env.get("ODYSSEUS_TOKEN", "").strip(),
            model=env.get("ODYSSEUS_MODEL", "").strip(),
            allowed_chat_ids=_parse_chat_ids(env.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
            reminder_secret=env.get("TELEGRAM_REMINDER_SECRET", "").strip(),
            pairing_code=env.get("TELEGRAM_PAIRING_CODE", "").strip(),
            timezone_name=(env.get("BOT_TIMEZONE", "") or "Europe/Istanbul").strip(),
            state_path=(env.get("BOT_STATE_PATH", "") or "/data/state.json").strip(),
            http_host=(env.get("BOT_HTTP_HOST", "") or "0.0.0.0").strip(),
            http_port=_int_env(env, "BOT_HTTP_PORT", 8088),
            history_turns=max(0, _int_env(env, "BOT_HISTORY_TURNS", 12)),
            history_chars=max(1_000, _int_env(env, "BOT_HISTORY_CHARS", 14_000)),
            request_timeout=max(30, _int_env(env, "BOT_REQUEST_TIMEOUT", 180)),
        )

    @property
    def configured(self) -> bool:
        return bool(self.telegram_token and self.odysseus_token)

    def problems(self) -> list[str]:
        """Human-readable configuration errors that must stop the bot."""
        issues = []
        if not self.telegram_token:
            issues.append("TELEGRAM_BOT_TOKEN is not set")
        elif not TELEGRAM_TOKEN_RE.fullmatch(self.telegram_token):
            issues.append("TELEGRAM_BOT_TOKEN does not look like a BotFather token (<digits>:<secret>)")
        if not self.odysseus_token:
            issues.append("ODYSSEUS_TOKEN (Coolify: TELEGRAM_ODYSSEUS_TOKEN) is not set")
        elif self.odysseus_token == self.telegram_token:
            issues.append(
                "ODYSSEUS_TOKEN is the Telegram bot token; it must be an Odysseus API token "
                "(ody_..., Settings → Integrations → Add Integration → Claude Agent)"
            )
        elif not self.odysseus_token.startswith("ody_"):
            issues.append("ODYSSEUS_TOKEN must be an Odysseus API token starting with ody_")
        return issues


def load_timezone(name: str) -> dt.tzinfo:
    """Resolve an IANA zone, falling back to a fixed offset when tzdata is missing."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        pass
    match = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", name.strip(), re.I)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        delta = dt.timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
        return dt.timezone(sign * delta)
    log.warning("Timezone %r unavailable; falling back to UTC+03:00", name)
    return dt.timezone(dt.timedelta(hours=3))


def format_offset(moment: dt.datetime) -> str:
    offset = moment.utcoffset() or dt.timedelta(0)
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def format_now(moment: dt.datetime, zone_name: str) -> str:
    return (
        f"{moment:%Y-%m-%d} {TR_WEEKDAYS[moment.weekday()]} {moment:%H:%M} "
        f"({zone_name}, UTC{format_offset(moment)})"
    )


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------


class State:
    """Small JSON state file: update offset, paired chats, per-chat history."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"offset": 0, "paired": [], "chats": {}}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:  # corrupt file: keep defaults, do not crash-loop
            log.warning("State file unreadable (%s); starting fresh", exc)
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)
        self.data.setdefault("paired", [])
        self.data.setdefault("chats", {})

    def save(self) -> None:
        with self.lock:
            directory = os.path.dirname(self.path) or "."
            try:
                os.makedirs(directory, exist_ok=True)
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except OSError as exc:
                log.warning("Could not save state: %s", exc)

    @property
    def offset(self) -> int:
        return int(self.data.get("offset") or 0)

    @offset.setter
    def offset(self, value: int) -> None:
        with self.lock:
            self.data["offset"] = int(value)

    @property
    def paired(self) -> set[int]:
        return {int(x) for x in self.data.get("paired", [])}

    def add_paired(self, chat_id: int) -> None:
        with self.lock:
            paired = self.paired
            paired.add(int(chat_id))
            self.data["paired"] = sorted(paired)

    def chat(self, chat_id: int) -> dict:
        with self.lock:
            chat = self.data["chats"].setdefault(str(chat_id), {})
            chat.setdefault("history", [])
            chat.setdefault("pending", [])
            return chat


# ---------------------------------------------------------------------------
# HTTP clients
# ---------------------------------------------------------------------------


class TelegramError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"Telegram API error {code}: {description}")
        self.code = code
        self.description = description


class Telegram:
    def __init__(self, token: str, api_base: str = TELEGRAM_API):
        self.token = token
        self.api_base = api_base.rstrip("/")

    def call(self, method: str, payload: Optional[dict] = None, timeout: float = 30) -> Any:
        # Never log this URL: it embeds the bot token.
        url = f"{self.api_base}/bot{self.token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read() or b"{}")
            except Exception:
                body = {}
            raise TelegramError(exc.code, str(body.get("description") or exc.reason)) from None
        if not body.get("ok"):
            raise TelegramError(int(body.get("error_code") or 0), str(body.get("description") or "unknown"))
        return body.get("result")

    def send(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            self.call(
                "sendMessage",
                {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            )

    def typing(self, chat_id: int) -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
        except Exception:
            pass


class OdysseusError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Odysseus HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _error_detail(raw: bytes) -> str:
    text = (raw or b"").decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return text[:300]
    if isinstance(parsed, dict):
        for key in ("detail", "error", "message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
    return text[:300]


class Odysseus:
    def __init__(self, base_url: str, token: str, model: str = "", timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.model = model
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        form: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or 60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise OdysseusError(exc.code, _error_detail(exc.read())) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise OdysseusError(0, f"Odysseus'a ulaşılamadı: {reason}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace")

    def chat(self, message: str) -> dict:
        """One stateless turn: Odysseus creates a new session for it."""
        body: dict[str, Any] = {"message": message}
        if self.model:
            body["model"] = self.model
        result = self.request("POST", "/api/v1/chat", body, timeout=self.timeout)
        return result if isinstance(result, dict) else {"response": str(result or "")}

    def delete_session(self, session_id: str) -> None:
        try:
            self.request("DELETE", f"/api/session/{urllib.parse.quote(session_id, safe='')}")
        except OdysseusError as exc:
            log.debug("Session cleanup failed: %s", exc)

    def capabilities(self) -> dict:
        result = self.request("GET", "/api/codex/capabilities")
        return result if isinstance(result, dict) else {}

    def todos(self, payload: dict) -> Any:
        return self.request("POST", "/api/codex/todos", payload)

    def list_todos(self) -> Any:
        return self.request("GET", "/api/codex/todos")

    def add_event(self, payload: dict) -> Any:
        return self.request("POST", "/api/codex/calendar/events", payload)

    def list_events(self, start: str, end: str) -> Any:
        query = urllib.parse.urlencode({"start": start, "end": end})
        return self.request("GET", f"/api/codex/calendar/events?{query}")

    def delete_event(self, uid: str) -> Any:
        return self.request("DELETE", f"/api/codex/calendar/events/{urllib.parse.quote(uid, safe='')}")

    def add_memory(self, text: str, category: str) -> Any:
        return self.request(
            "POST", "/api/codex/memory", {"text": text, "category": category, "source": "user"}
        )

    def list_memory(self) -> Any:
        return self.request("GET", "/api/codex/memory")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def split_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
ACTION_FENCE_RE = re.compile(r"```[ \t]*(odysseus|json)?[ \t]*\n?(.*?)```", re.S | re.I)


def _coerce_actions(obj: Any) -> Optional[list[dict]]:
    if isinstance(obj, dict) and isinstance(obj.get("actions"), list):
        items = obj["actions"]
    elif isinstance(obj, dict) and isinstance(obj.get("type"), str):
        items = [obj]
    elif isinstance(obj, list):
        items = obj
    else:
        return None
    actions = [a for a in items if isinstance(a, dict) and isinstance(a.get("type"), str)]
    return actions if actions or items == [] else None


def extract_actions(reply: str) -> tuple[str, list[dict]]:
    """Split a model reply into user-visible text and requested actions."""
    text = THINK_RE.sub("", reply or "")
    actions: list[dict] = []
    for match in list(ACTION_FENCE_RE.finditer(text)):
        lang = (match.group(1) or "").lower()
        try:
            obj = json.loads(match.group(2).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        found = _coerce_actions(obj)
        if found is None or (lang != "odysseus" and not found):
            continue
        actions.extend(found)
        text = text.replace(match.group(0), "")
    if not actions:
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                found = _coerce_actions(json.loads(stripped))
            except (json.JSONDecodeError, ValueError):
                found = None
            if found:
                actions.extend(found)
                text = ""
    return text.strip(), actions[:MAX_ACTIONS_PER_REPLY]


def _clean_markdown(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")


def _parse_iso(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _format_when(value: Any, tz: dt.tzinfo, all_day: bool = False) -> str:
    moment = _parse_iso(value)
    if moment is None:
        return str(value or "")
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    day = f"{moment:%d.%m} {TR_WEEKDAYS_SHORT[moment.weekday()]}"
    if all_day or (len(str(value)) <= 10):
        return f"{day} (tüm gün)"
    return f"{day} {moment:%H:%M}"


def _todo_text(result: Any) -> str:
    if isinstance(result, dict):
        if result.get("error"):
            return f"⚠️ {result['error']}"
        text = result.get("results") or result.get("response") or ""
        if isinstance(text, list):
            text = "\n".join(str(x) for x in text)
        return _clean_markdown(str(text)).strip()
    return _clean_markdown(str(result or "")).strip()


def _events_of(result: Any) -> list[dict]:
    if isinstance(result, dict):
        events = result.get("events")
    else:
        events = result
    return [e for e in (events or []) if isinstance(e, dict)]


def format_events(events: list[dict], tz: dt.tzinfo) -> str:
    if not events:
        return "Bu aralıkta etkinlik yok."

    def sort_key(event: dict) -> str:
        return str(event.get("dtstart") or event.get("start") or "")

    lines = []
    for event in sorted(events, key=sort_key)[:30]:
        when = _format_when(event.get("dtstart") or event.get("start"), tz, bool(event.get("all_day")))
        title = event.get("summary") or event.get("title") or "(başlıksız)"
        location = f" – {event['location']}" if event.get("location") else ""
        lines.append(f"• {when} {title}{location}")
    return "\n".join(lines)


def _memories_of(result: Any) -> list[dict]:
    if isinstance(result, dict):
        result = result.get("memory") or result.get("memories") or []
    return [m for m in (result or []) if isinstance(m, dict)]


def format_memories(memories: list[dict], limit: int = 20) -> str:
    if not memories:
        return "Kayıtlı hafıza notu yok."
    ordered = sorted(memories, key=lambda m: m.get("timestamp") or 0, reverse=True)
    return "\n".join(f"• {m.get('text', '').strip()}" for m in ordered[:limit])


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ActionResult:
    user: str  # shown in Telegram
    llm: str  # fed back to the model on the next turn


class ActionRunner:
    def __init__(self, ody: Odysseus, tz: dt.tzinfo):
        self.ody = ody
        self.tz = tz

    def run(self, actions: Iterable[dict]) -> list[ActionResult]:
        results = []
        for action in actions:
            kind = str(action.get("type") or "").strip().lower()
            handler = getattr(self, f"_do_{kind}", None)
            if handler is None:
                results.append(ActionResult(f"⚠️ Bilinmeyen işlem: {kind}", f"{kind}: bilinmeyen işlem"))
                continue
            try:
                results.append(handler(action))
            except OdysseusError as exc:
                results.append(
                    ActionResult(f"⚠️ {kind} başarısız: {exc.detail}", f"{kind}: HATA {exc.status} {exc.detail}")
                )
            except Exception as exc:  # never let one bad action kill the turn
                log.exception("Action %s crashed", kind)
                results.append(ActionResult(f"⚠️ {kind} başarısız: {exc}", f"{kind}: HATA {exc}"))
        return results

    # -- todos -------------------------------------------------------------

    def _do_todo_add(self, action: dict) -> ActionResult:
        title = str(action.get("title") or action.get("text") or "").strip()
        if not title:
            return ActionResult("⚠️ Hatırlatma başlığı boş.", "todo_add: HATA başlık boş")
        payload: dict[str, Any] = {"action": "add", "title": title}
        for key in ("due_date", "content"):
            if action.get(key):
                payload[key] = str(action[key])
        items = action.get("checklist_items") or action.get("items")
        if isinstance(items, list) and items:
            payload["checklist_items"] = [str(i) for i in items][:50]
        result = self.ody.todos(payload)
        detail = _todo_text(result)
        when = f" ({_format_when(payload['due_date'], self.tz)})" if payload.get("due_date") else ""
        icon = "⏰" if payload.get("due_date") else "✅"
        return ActionResult(f"{icon} Eklendi: {title}{when}", f"todo_add: {detail}")

    def _do_todo_list(self, action: dict) -> ActionResult:
        text = _todo_text(self.ody.list_todos()) or "Liste boş."
        if text.lower().startswith("no notes"):
            text = "Hatırlatma veya yapılacak yok."
        return ActionResult(f"📋 Yapılacaklar:\n{text}", f"todo_list:\n{text}")

    def _do_todo_update(self, action: dict) -> ActionResult:
        note_id = str(action.get("id") or "").strip()
        if not note_id:
            return ActionResult("⚠️ Güncellenecek kaydın id'si yok.", "todo_update: HATA id yok")
        payload = {"action": "update", "id": note_id}
        for key in ("title", "due_date", "content", "pinned", "archived"):
            if key in action:
                payload[key] = action[key]
        detail = _todo_text(self.ody.todos(payload))
        return ActionResult(f"✏️ Güncellendi: {detail or note_id}", f"todo_update: {detail}")

    def _do_todo_delete(self, action: dict) -> ActionResult:
        note_id = str(action.get("id") or "").strip()
        if not note_id:
            return ActionResult("⚠️ Silinecek kaydın id'si yok.", "todo_delete: HATA id yok")
        detail = _todo_text(self.ody.todos({"action": "delete", "id": note_id}))
        return ActionResult(f"🗑️ {detail or 'Silindi'}", f"todo_delete: {detail}")

    # -- calendar ----------------------------------------------------------

    def _do_calendar_add(self, action: dict) -> ActionResult:
        summary = str(action.get("summary") or action.get("title") or "").strip()
        dtstart = str(action.get("dtstart") or action.get("start") or "").strip()
        if not summary or not dtstart:
            return ActionResult("⚠️ Etkinlik için başlık ve başlangıç zamanı gerekli.", "calendar_add: HATA eksik alan")
        payload: dict[str, Any] = {"summary": summary, "dtstart": dtstart}
        if action.get("dtend") or action.get("end"):
            payload["dtend"] = str(action.get("dtend") or action.get("end"))
        payload["all_day"] = bool(action.get("all_day", False))
        for key in ("location", "description", "rrule"):
            if action.get(key):
                payload[key] = str(action[key])
        result = self.ody.add_event(payload)
        uid = result.get("uid") if isinstance(result, dict) else None
        when = _format_when(dtstart, self.tz, payload["all_day"])
        return ActionResult(
            f"📅 Takvime eklendi: {summary} ({when})",
            f"calendar_add: eklendi uid={uid or '?'} {json.dumps(result, ensure_ascii=False)[:300]}",
        )

    def _do_calendar_list(self, action: dict) -> ActionResult:
        now = dt.datetime.now(self.tz)
        start = _parse_iso(action.get("start")) or now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = _parse_iso(action.get("end")) or start + dt.timedelta(days=7)
        if start.tzinfo is None:
            start = start.replace(tzinfo=self.tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=self.tz)
        events = _events_of(self.ody.list_events(start.isoformat(), end.isoformat()))
        compact = [
            {k: e.get(k) for k in ("uid", "summary", "dtstart", "dtend", "all_day", "location") if k in e}
            for e in events[:30]
        ]
        return ActionResult(
            f"📅 Takvim:\n{format_events(events, self.tz)}",
            f"calendar_list: {json.dumps(compact, ensure_ascii=False)}",
        )

    def _do_calendar_delete(self, action: dict) -> ActionResult:
        uid = str(action.get("uid") or action.get("id") or "").strip()
        if not uid:
            return ActionResult("⚠️ Silinecek etkinliğin uid'i yok.", "calendar_delete: HATA uid yok")
        self.ody.delete_event(uid)
        return ActionResult("🗑️ Etkinlik silindi.", f"calendar_delete: silindi {uid}")

    # -- memory ------------------------------------------------------------

    def _do_memory_add(self, action: dict) -> ActionResult:
        text = str(action.get("text") or "").strip()
        if not text:
            return ActionResult("⚠️ Hafızaya eklenecek metin boş.", "memory_add: HATA metin boş")
        category = str(action.get("category") or "fact").strip().lower()
        self.ody.add_memory(text[:5000], category)
        return ActionResult(f"🧠 Hafızaya eklendi: {text[:200]}", f"memory_add: eklendi ({category})")

    def _do_memory_list(self, action: dict) -> ActionResult:
        memories = _memories_of(self.ody.list_memory())
        compact = [{"id": m.get("id"), "text": m.get("text")} for m in memories[:30]]
        return ActionResult(
            f"🧠 Hafıza:\n{format_memories(memories)}",
            f"memory_list: {json.dumps(compact, ensure_ascii=False)}",
        )


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class _Typing:
    """Keep Telegram's "typing…" indicator alive while a slow call runs."""

    def __init__(self, tg: Telegram, chat_id: int, interval: float = 4.5):
        self.tg = tg
        self.chat_id = chat_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tg.typing(self.chat_id)
            self._stop.wait(self.interval)

    def __enter__(self) -> "_Typing":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()


class Bot:
    def __init__(
        self,
        cfg: Config,
        state: State,
        tg: Optional[Telegram] = None,
        ody: Optional[Odysseus] = None,
        clock: Optional[Callable[[], dt.datetime]] = None,
    ):
        self.cfg = cfg
        self.state = state
        self.tz = load_timezone(cfg.timezone_name)
        self.tg = tg if tg is not None else (Telegram(cfg.telegram_token) if cfg.telegram_token else None)
        self.ody = ody if ody is not None else Odysseus(
            cfg.odysseus_url, cfg.odysseus_token, cfg.model, cfg.request_timeout
        )
        self.runner = ActionRunner(self.ody, self.tz)
        self.clock = clock or (lambda: dt.datetime.now(self.tz))
        self.chat_locks: dict[int, threading.Lock] = {}
        self.polling = False
        self.pairing_failures = 0
        self.pairing_code = ""
        if self.pairing_active:
            self.pairing_code = cfg.pairing_code or secrets.token_hex(4).upper()

    # -- authorization -----------------------------------------------------

    @property
    def allowed_chats(self) -> set[int]:
        return set(self.cfg.allowed_chat_ids) | self.state.paired

    @property
    def pairing_active(self) -> bool:
        return not self.allowed_chats and self.pairing_failures < PAIRING_MAX_FAILURES

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self.allowed_chats

    def _try_pair(self, chat_id: int, chat_type: str, code: str) -> str:
        if not self.pairing_active or not self.pairing_code:
            return "Eşleştirme kapalı."
        if chat_type != "private":
            return "Eşleştirme yalnızca bota özel mesajla yapılabilir."
        if secrets.compare_digest(code.strip().upper(), self.pairing_code.upper()):
            self.state.add_paired(chat_id)
            self.state.save()
            log.info("Chat %s paired; pairing is now closed", chat_id)
            return "✅ Eşleştirildi! Artık bu sohbetten Odysseus'u kullanabilirsin.\n\n" + HELP_TEXT
        self.pairing_failures += 1
        log.warning("Wrong pairing code from chat %s (%d/%d)", chat_id, self.pairing_failures, PAIRING_MAX_FAILURES)
        if self.pairing_failures >= PAIRING_MAX_FAILURES:
            log.warning("Too many wrong pairing codes; pairing disabled until restart")
        return "❌ Kod hatalı."

    # -- update handling ---------------------------------------------------

    def handle_update(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        chat_type = str(chat.get("type") or "")
        text = message.get("text")
        command, args = parse_command(text)

        if command in ("eslestir", "pair"):
            self.reply(chat_id, self._try_pair(chat_id, chat_type, args))
            return
        if command in ("kimim", "id"):
            self.reply(chat_id, f"Chat ID: {chat_id}")
            return
        if not self.is_allowed(chat_id):
            log.info("Ignoring message from unauthorized chat %s", chat_id)
            if self.pairing_active and chat_type == "private":
                self.reply(
                    chat_id,
                    "Bu bot henüz eşleştirilmedi. telegram-bot servisinin loglarındaki kodu "
                    f"\"/eslestir KOD\" şeklinde gönder.\n(Chat ID: {chat_id})",
                )
            return
        if not isinstance(text, str) or not text.strip():
            self.reply(chat_id, "Şimdilik yalnızca metin mesajlarını anlayabiliyorum.")
            return

        lock = self.chat_locks.setdefault(chat_id, threading.Lock())
        with lock:
            if command:
                self.handle_command(chat_id, command, args)
            else:
                self.handle_text(chat_id, text.strip())

    def handle_command(self, chat_id: int, command: str, args: str) -> None:
        if command in ("start", "yardim", "help"):
            self.reply(chat_id, HELP_TEXT)
        elif command in ("yeni", "new", "reset"):
            chat = self.state.chat(chat_id)
            chat.update({"history": [], "pending": []})
            self.state.save()
            self.reply(chat_id, "🆕 Yeni sohbet başladı.")
        elif command in ("yapilacaklar", "todo", "todos", "hatirlatmalar"):
            self.reply_results(chat_id, self.runner.run([{"type": "todo_list"}]))
        elif command in ("takvim", "calendar"):
            days = int(args) if args.strip().isdigit() else 7
            days = max(1, min(days, 90))
            start = self.clock().replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + dt.timedelta(days=days)
            action = {"type": "calendar_list", "start": start.isoformat(), "end": end.isoformat()}
            self.reply_results(chat_id, self.runner.run([action]))
        elif command in ("hafiza", "memory"):
            self.reply_results(chat_id, self.runner.run([{"type": "memory_list"}]))
        elif command in ("durum", "status"):
            self.reply(chat_id, self.status_text(chat_id))
        else:
            # Unknown slash command: let the model handle it as plain text.
            self.handle_text(chat_id, f"/{command} {args}".strip())

    def status_text(self, chat_id: Optional[int] = None) -> str:
        lines = [f"Odysseus: {self.cfg.odysseus_url}", f"Model: {self.cfg.model or '(Odysseus varsayılanı)'}"]
        if chat_id is not None:
            turns = len(self.state.chat(chat_id)["history"])
            lines.append(f"Sohbet bağlamı: {turns}/{self.cfg.history_turns} tur")
        try:
            caps = self.ody.capabilities()
            scopes = ", ".join(sorted(caps.get("token_scopes") or [])) or "-"
            lines.append(f"Bağlantı: ✅ (yetkiler: {scopes})")
        except OdysseusError as exc:
            lines.append(f"Bağlantı: ❌ {exc.detail}")
        return "\n".join(lines)

    def handle_text(self, chat_id: int, text: str) -> None:
        chat = self.state.chat(chat_id)
        try:
            with _Typing(self.tg, chat_id):
                response = self.ody.chat(self.build_message(chat, text))
        except OdysseusError as exc:
            log.warning("Chat failed for %s: HTTP %s", chat_id, exc.status)
            self.reply(chat_id, f"⚠️ Odysseus yanıt veremedi ({exc.status or 'bağlantı'}): {exc.detail}")
            return
        if response.get("session_id"):
            # The turn's session was only a vehicle for this one request.
            self.ody.delete_session(str(response["session_id"]))

        visible, actions = extract_actions(str(response.get("response") or ""))
        results = self.runner.run(actions) if actions else []
        pending = "\n".join(r.llm for r in results)
        chat["pending"] = [pending[:MAX_PENDING_CHARS]] if pending else []

        shown = "\n\n".join(p for p in (visible, "\n".join(r.user for r in results)) if p)
        chat["history"].append({"u": text[:MAX_TURN_USER_CHARS], "a": shown[:MAX_TURN_ASSISTANT_CHARS]})
        self._trim_history(chat)
        self.state.save()
        self.reply(chat_id, shown or "✓")

    def _trim_history(self, chat: dict) -> None:
        history = chat["history"][-self.cfg.history_turns:] if self.cfg.history_turns else []
        while history and sum(len(t["u"]) + len(t["a"]) for t in history) > self.cfg.history_chars:
            history = history[1:]
        chat["history"] = history

    def build_message(self, chat: dict, text: str) -> str:
        """Compose one self-contained turn: protocol, recent context, clock, new message."""
        tail = []
        if chat.get("pending"):
            tail.append("[Önceki işlemlerin sonuçları]\n" + "\n".join(chat["pending"]))
        tail.append(f"[Şu an: {format_now(self.clock(), self.cfg.timezone_name)}]")
        tail.append("[Yeni mesaj]\n" + text)

        history = list(chat.get("history") or [])
        while True:
            parts = [SYSTEM_PROMPT]
            if history:
                lines = []
                for turn in history:
                    lines.append(f"Kullanıcı: {turn['u']}")
                    lines.append(f"Asistan: {turn['a']}")
                parts.append("[Önceki konuşma]\n" + "\n".join(lines))
            message = "\n\n".join(parts + tail)
            if len(message) <= MAX_CHAT_MESSAGE or not history:
                return message[:MAX_CHAT_MESSAGE]
            history = history[1:]

    # -- output ------------------------------------------------------------

    def reply(self, chat_id: int, text: str) -> None:
        if self.tg is None:
            return
        try:
            self.tg.send(chat_id, text)
        except Exception as exc:
            log.warning("Could not send message to %s: %s", chat_id, exc)

    def reply_results(self, chat_id: int, results: list[ActionResult]) -> None:
        self.reply(chat_id, "\n\n".join(r.user for r in results) or "✓")

    def notify(self, text: str) -> int:
        """Send a notification to every authorized chat. Returns delivery count."""
        delivered = 0
        for chat_id in sorted(self.allowed_chats):
            try:
                self.tg.send(chat_id, text)
                delivered += 1
            except Exception as exc:
                log.warning("Reminder delivery to %s failed: %s", chat_id, exc)
        return delivered

    # -- main loop ---------------------------------------------------------

    def announce(self) -> None:
        try:
            me = self.tg.call("getMe")
            log.info("Connected to Telegram as @%s", (me or {}).get("username", "?"))
        except Exception as exc:
            log.error("Telegram getMe failed: %s", exc)
        try:
            self.tg.call("setMyCommands", {"commands": BOT_COMMANDS})
        except Exception as exc:
            log.debug("setMyCommands failed: %s", exc)
        if self.pairing_active:
            log.warning(
                "PAIRING MODE: send \"/eslestir %s\" to the bot from your Telegram account "
                "(or set TELEGRAM_ALLOWED_CHAT_IDS).",
                self.pairing_code,
            )
        else:
            log.info("Authorized chats: %s", ", ".join(str(c) for c in sorted(self.allowed_chats)))

    def poll_forever(self, stop: threading.Event) -> None:
        """Long-poll Telegram until stopped. Returns early on a token conflict."""
        self.announce()
        self.polling = True
        try:
            self._poll_loop(stop)
        finally:
            self.polling = False

    def _poll_loop(self, stop: threading.Event) -> None:
        backoff = 1.0
        conflicts = 0
        while not stop.is_set():
            try:
                updates = self.tg.call(
                    "getUpdates",
                    {"offset": self.state.offset, "timeout": 25, "allowed_updates": ["message"]},
                    timeout=40,
                )
                backoff = 1.0
                conflicts = 0
            except TelegramError as exc:
                if exc.code == 409 and "other getupdates" in exc.description.lower():
                    conflicts += 1
                    if conflicts >= MAX_POLL_CONFLICTS:
                        log.error(
                            "Another program is polling this bot token, so "
                            "the two would keep disconnecting each other. Stopped polling. Create a "
                            "separate bot with @BotFather for Odysseus, set it as TELEGRAM_BOT_TOKEN "
                            "and redeploy."
                        )
                        return
                    log.warning("Telegram polling conflict %d/%d: %s", conflicts, MAX_POLL_CONFLICTS, exc)
                elif exc.code == 409 and "webhook" in exc.description.lower():
                    log.warning("A Telegram webhook is set; removing it so long polling works")
                    try:
                        self.tg.call("deleteWebhook")
                    except Exception:
                        pass
                elif exc.code == 401:
                    log.error("Telegram rejected the bot token (401). Check TELEGRAM_BOT_TOKEN.")
                    backoff = 60.0
                else:
                    log.warning("getUpdates failed: %s", exc)
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            except Exception as exc:
                log.warning("getUpdates network error: %s", exc)
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            for update in updates or []:
                # Advance the offset first: a poison update must not loop forever.
                self.state.offset = int(update.get("update_id", 0)) + 1
                self.state.save()
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("Failed to handle update %s", update.get("update_id"))


def parse_command(text: Any) -> tuple[str, str]:
    if not isinstance(text, str):
        return "", ""
    match = re.match(r"^/([A-Za-z0-9_]+)(?:@\w+)?(?:\s+(.*))?$", text.strip(), re.S)
    if not match:
        return "", ""
    return match.group(1).lower(), (match.group(2) or "").strip()


# ---------------------------------------------------------------------------
# Internal HTTP endpoint (reminder webhook + health)
# ---------------------------------------------------------------------------


def make_http_handler(bot: Bot) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "odysseus-telegram"

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            secret = bot.cfg.reminder_secret
            if not secret:
                return True
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.lower().startswith("bearer ") else header
            return secrets.compare_digest(supplied.strip(), secret)

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send(
                    200,
                    {
                        "ok": True,
                        "configured": not bot.cfg.problems(),
                        "polling": bot.polling,
                        "pairing": bot.pairing_active,
                    },
                )
            elif path in ("/reminder", "/notify"):
                # Lets the Odysseus "API Integration" Test button succeed.
                if not self._authorized():
                    self._send(401, {"error": "unauthorized"})
                    return
                self._send(200, {"ok": True, "hint": "POST {\"title\", \"message\"} here"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > REMINDER_MAX_BODY:
                self._send(400, {"error": "bad body"})
                return
            # Drain the body before answering so error replies don't reset the
            # connection on a client that is still sending.
            raw = self.rfile.read(length)
            if self.path.split("?", 1)[0] not in ("/reminder", "/notify"):
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._send(400, {"error": "invalid json"})
                return
            if bot.tg is None or not bot.allowed_chats:
                self._send(503, {"error": "telegram not configured or no paired chat"})
                return
            # Accept the template we document ({"title", "message"}) and the
            # Odysseus placeholder shape ({"content": "..."}).
            title = str(payload.get("title") or payload.get("content") or "Hatırlatma").strip()[:300]
            message = str(payload.get("message") or "").strip()[:3500]
            text = f"⏰ {title}"
            if message and message != title:
                text += f"\n\n{message}"
            delivered = bot.notify(text)
            self._send(200 if delivered else 502, {"ok": bool(delivered), "delivered": delivered})

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("http: " + fmt, *args)

    return Handler


def start_http_server(bot: Bot) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bot.cfg.http_host, bot.cfg.http_port), make_http_handler(bot))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    log.info("Internal HTTP endpoint on %s:%s (/health, /reminder)", bot.cfg.http_host, bot.cfg.http_port)
    return server


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BOT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    def _terminate(signum, frame):  # noqa: ARG001
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)

    cfg = Config.from_env()
    state = State(cfg.state_path)
    bot = Bot(cfg, state)
    start_http_server(bot)
    stop = threading.Event()

    problems = cfg.problems()
    if problems:
        level = logging.WARNING if not cfg.telegram_token and not cfg.odysseus_token else logging.ERROR
        for problem in problems:
            log.log(level, "Idle: %s.", problem)
        log.log(level, "Fix the variables in Coolify and redeploy to enable the bot.")
        stop.wait()
        return 0
    try:
        bot.poll_forever(stop)
        # Polling gave up (token conflict); keep the health endpoint up and idle
        # instead of exiting into a restart loop that would poll again.
        stop.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
