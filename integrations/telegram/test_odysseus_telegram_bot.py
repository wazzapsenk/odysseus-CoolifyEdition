"""Offline tests for the Telegram <-> Odysseus bridge.

Run with:  python -m pytest integrations/telegram -q
(or:       python -m unittest discover -s integrations/telegram)
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import odysseus_telegram_bot as botmod  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=3))
NOW = dt.datetime(2026, 10, 6, 23, 30, tzinfo=TZ)


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def typing(self, chat_id):
        pass

    def call(self, method, payload=None, timeout=30):
        return {}


class FakeOdysseus(botmod.Odysseus):
    """Routes requests to canned handlers and records every call."""

    def __init__(self, chat_replies=None):
        super().__init__("http://odysseus:7000", "ody_test", model="gpt-5.6-sol")
        self.calls: list[tuple[str, str, object]] = []
        self.chat_replies = list(chat_replies or [])
        self.created = 0
        self.deleted: list[str] = []

    def request(self, method, path, body=None, form=None, timeout=None):
        self.calls.append((method, path, body if body is not None else form))
        if path == "/api/v1/chat":
            assert "session" not in body, "bridge must never resume a session"
            self.created += 1
            reply = self.chat_replies.pop(0) if self.chat_replies else "tamam"
            return {"response": reply, "session_id": f"sess-{self.created}", "model": body.get("model")}
        if path.startswith("/api/session/") and method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[-1])
            return {"status": "deleted"}
        if path == "/api/codex/todos" and method == "GET":
            return {"results": "- [5de9478b] **Diş hekimi**", "exit_code": 0}
        if path == "/api/codex/todos":
            if body.get("action") == "add":
                return {"response": f"Reminder created: \"{body['title']}\" (id: 5de9478b)", "note_id": "5de9478b-x"}
            if body.get("action") == "delete":
                return {"response": "Deleted note: \"Diş hekimi\""}
            return {"response": "ok"}
        if path.startswith("/api/codex/calendar/events") and method == "GET":
            return {"events": [{"uid": "ev-1", "summary": "Toplantı", "dtstart": "2026-10-09T15:00:00+03:00"}]}
        if path == "/api/codex/calendar/events":
            return {"uid": "ev-new", "summary": body["summary"]}
        if path == "/api/codex/memory" and method == "GET":
            return {"memory": [{"id": "m1", "text": "Kahveyi şekersiz içer", "timestamp": 1}]}
        if path == "/api/codex/memory":
            return {"status": "ok"}
        if path == "/api/codex/capabilities":
            return {"token_scopes": ["chat", "todos:read"]}
        raise AssertionError(f"unexpected request {method} {path}")

    def chat_messages(self):
        return [c[2]["message"] for c in self.calls if c[1] == "/api/v1/chat"]


def make_bot(allowed=(111,), replies=None, state_dir=None, **cfg_kwargs):
    state_dir = state_dir or tempfile.mkdtemp()
    cfg = botmod.Config(
        telegram_token="123:abc",
        odysseus_token="ody_test",
        model="gpt-5.6-sol",
        allowed_chat_ids=set(allowed),
        state_path=os.path.join(state_dir, "state.json"),
        timezone_name="UTC+03:00",
        **cfg_kwargs,
    )
    state = botmod.State(cfg.state_path)
    tg = FakeTelegram()
    ody = FakeOdysseus(replies)
    bot = botmod.Bot(cfg, state, tg=tg, ody=ody, clock=lambda: NOW)
    return bot, tg, ody


def text_update(chat_id, text, chat_type="private", update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}


class ExtractActionsTests(unittest.TestCase):
    def test_odysseus_fence(self):
        reply = 'Tamam, ekliyorum.\n```odysseus\n{"actions": [{"type": "todo_add", "title": "X"}]}\n```'
        visible, actions = botmod.extract_actions(reply)
        self.assertEqual(visible, "Tamam, ekliyorum.")
        self.assertEqual(actions, [{"type": "todo_add", "title": "X"}])

    def test_json_fence_with_actions_and_single_action_object(self):
        _, actions = botmod.extract_actions('```json\n{"actions":[{"type":"todo_list"}]}\n```')
        self.assertEqual(actions, [{"type": "todo_list"}])
        _, actions = botmod.extract_actions('```odysseus\n{"type":"memory_list"}\n```')
        self.assertEqual(actions, [{"type": "memory_list"}])

    def test_plain_json_code_is_left_alone(self):
        reply = 'Örnek:\n```json\n{"name": "x"}\n```'
        visible, actions = botmod.extract_actions(reply)
        self.assertEqual(actions, [])
        self.assertIn('{"name": "x"}', visible)

    def test_bare_json_reply_and_think_tags(self):
        visible, actions = botmod.extract_actions('<think>hmm</think>{"actions":[{"type":"todo_list"}]}')
        self.assertEqual(visible, "")
        self.assertEqual(actions, [{"type": "todo_list"}])

    def test_invalid_json_ignored_and_action_cap(self):
        visible, actions = botmod.extract_actions("```odysseus\n{not json}\n```")
        self.assertEqual(actions, [])
        many = json.dumps({"actions": [{"type": "todo_list"}] * 20})
        _, actions = botmod.extract_actions(f"```odysseus\n{many}\n```")
        self.assertEqual(len(actions), botmod.MAX_ACTIONS_PER_REPLY)


class HelperTests(unittest.TestCase):
    def test_split_message(self):
        text = ("satır\n" * 2000).strip()
        chunks = botmod.split_message(text, limit=500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("".join(c.replace("\n", "") for c in chunks), text.replace("\n", ""))
        self.assertEqual(botmod.split_message(""), [])

    def test_parse_command(self):
        self.assertEqual(botmod.parse_command("/takvim 3"), ("takvim", "3"))
        self.assertEqual(botmod.parse_command("/yeni@odysseus_bot"), ("yeni", ""))
        self.assertEqual(botmod.parse_command("merhaba"), ("", ""))
        self.assertEqual(botmod.parse_command(None), ("", ""))

    def test_timezone_fallback(self):
        tz = botmod.load_timezone("UTC+05:30")
        self.assertEqual(dt.datetime(2026, 1, 1, tzinfo=tz).utcoffset(), dt.timedelta(hours=5, minutes=30))
        tz = botmod.load_timezone("Not/AZone")
        self.assertEqual(dt.datetime(2026, 1, 1, tzinfo=tz).utcoffset(), dt.timedelta(hours=3))

    def test_format_now(self):
        self.assertEqual(
            botmod.format_now(NOW, "Europe/Istanbul"),
            "2026-10-06 Salı 23:30 (Europe/Istanbul, UTC+03:00)",
        )

    def test_config_from_env(self):
        cfg = botmod.Config.from_env(
            {"TELEGRAM_BOT_TOKEN": "t", "ODYSSEUS_TOKEN": "o", "TELEGRAM_ALLOWED_CHAT_IDS": "1, 2 x"}
        )
        self.assertEqual(cfg.allowed_chat_ids, {1, 2})
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.odysseus_url, "http://odysseus:7000")
        self.assertFalse(botmod.Config.from_env({}).configured)


class AuthorizationTests(unittest.TestCase):
    def test_unauthorized_chat_is_ignored_when_not_pairing(self):
        bot, tg, ody = make_bot(allowed=(111,))
        bot.handle_update(text_update(999, "merhaba"))
        self.assertEqual(tg.sent, [])
        self.assertEqual(ody.calls, [])

    def test_pairing_flow(self):
        bot, tg, ody = make_bot(allowed=(), pairing_code="ABCD1234")
        self.assertTrue(bot.pairing_active)
        bot.handle_update(text_update(555, "merhaba"))
        self.assertIn("eşleştirilmedi", tg.sent[-1][1])
        bot.handle_update(text_update(555, "/eslestir yanlis"))
        self.assertIn("hatalı", tg.sent[-1][1])
        bot.handle_update(text_update(777, "/eslestir abcd1234", chat_type="group"))
        self.assertIn("özel mesaj", tg.sent[-1][1])
        bot.handle_update(text_update(555, "/eslestir abcd1234"))
        self.assertIn("Eşleştirildi", tg.sent[-1][1])
        self.assertTrue(bot.is_allowed(555))
        self.assertFalse(bot.pairing_active)
        # Pairing survives a restart via the state file.
        reloaded = botmod.State(bot.cfg.state_path)
        self.assertIn(555, reloaded.paired)
        # A second chat can no longer pair.
        bot.handle_update(text_update(556, "/eslestir abcd1234"))
        self.assertIn("kapalı", tg.sent[-1][1])
        self.assertFalse(bot.is_allowed(556))

    def test_pairing_locks_after_failures(self):
        bot, tg, _ = make_bot(allowed=(), pairing_code="ABCD1234")
        for _ in range(botmod.PAIRING_MAX_FAILURES):
            bot.handle_update(text_update(555, "/eslestir nope"))
        self.assertFalse(bot.pairing_active)
        bot.handle_update(text_update(555, "/eslestir ABCD1234"))
        self.assertFalse(bot.is_allowed(555))


class ConversationTests(unittest.TestCase):
    def test_each_turn_is_self_contained_and_cleaned_up(self):
        bot, tg, ody = make_bot(replies=["Merhaba!", "İyiyim."])
        bot.handle_update(text_update(111, "selam"))
        first = ody.chat_messages()[0]
        self.assertTrue(first.startswith("[ODYSSEUS TELEGRAM KÖPRÜSÜ"))
        self.assertIn("[Şu an: 2026-10-06 Salı 23:30", first)
        self.assertTrue(first.endswith("[Yeni mesaj]\nselam"))
        self.assertNotIn("[Önceki konuşma]\n", first)
        self.assertEqual(tg.sent[-1], (111, "Merhaba!"))
        self.assertEqual(ody.deleted, ["sess-1"])
        # Second turn: fresh session, protocol again, previous exchange as context.
        bot.handle_update(text_update(111, "nasılsın", update_id=2))
        second_call = [c for c in ody.calls if c[1] == "/api/v1/chat"][1]
        self.assertIn("KÖPRÜSÜ", second_call[2]["message"])
        self.assertIn("[Önceki konuşma]\nKullanıcı: selam\nAsistan: Merhaba!", second_call[2]["message"])
        self.assertEqual(second_call[2]["model"], "gpt-5.6-sol")
        self.assertEqual(ody.deleted, ["sess-1", "sess-2"])
        # History survives a restart.
        self.assertEqual(len(botmod.State(bot.cfg.state_path).chat(111)["history"]), 2)

    def test_actions_execute_and_results_feed_next_turn(self):
        reply = (
            "Hatırlatmayı kuruyorum.\n```odysseus\n"
            '{"actions": [{"type": "todo_add", "title": "Diş hekimi", "due_date": "2026-10-07T09:00:00+03:00"},'
            ' {"type": "calendar_add", "summary": "Toplantı", "dtstart": "2026-10-09T15:00:00+03:00"},'
            ' {"type": "memory_add", "text": "Kahveyi şekersiz içer", "category": "preference"}]}\n```'
        )
        bot, tg, ody = make_bot(replies=[reply, "Rica ederim"])
        bot.handle_update(text_update(111, "yarın 9'da diş hekimi, cuma 15 toplantı, şekersiz kahve"))
        out = tg.sent[-1][1]
        self.assertIn("Hatırlatmayı kuruyorum.", out)
        self.assertIn("⏰ Eklendi: Diş hekimi (07.10 Çar 09:00)", out)
        self.assertIn("📅 Takvime eklendi: Toplantı (09.10 Cum 15:00)", out)
        self.assertIn("🧠 Hafızaya eklendi", out)
        self.assertNotIn("```", out)
        todo_add = [c for c in ody.calls if c[1] == "/api/codex/todos" and c[0] == "POST"][0]
        self.assertEqual(todo_add[2]["due_date"], "2026-10-07T09:00:00+03:00")
        bot.handle_update(text_update(111, "teşekkürler", update_id=2))
        follow_up = ody.chat_messages()[1]
        self.assertIn("[Önceki işlemlerin sonuçları]\n", follow_up)
        self.assertIn("todo_add: Reminder created", follow_up)
        # Results are consumed once.
        bot.handle_update(text_update(111, "tamam", update_id=3))
        self.assertNotIn("[Önceki işlemlerin sonuçları]\n", ody.chat_messages()[2])

    def test_unknown_and_failing_actions_are_reported(self):
        reply = '```odysseus\n{"actions": [{"type": "launch_rocket"}, {"type": "todo_delete"}]}\n```'
        bot, tg, _ = make_bot(replies=[reply])
        bot.handle_update(text_update(111, "x"))
        out = tg.sent[-1][1]
        self.assertIn("Bilinmeyen işlem: launch_rocket", out)
        self.assertIn("id'si yok", out)

    def test_history_window_by_turns(self):
        bot, tg, ody = make_bot(history_turns=2)
        for i in range(4):
            bot.handle_update(text_update(111, f"mesaj {i}", update_id=i + 1))
        last = ody.chat_messages()[-1]
        self.assertNotIn("mesaj 0", last)
        self.assertNotIn("Kullanıcı: mesaj 0", last)
        self.assertIn("Kullanıcı: mesaj 1", last)
        self.assertIn("Kullanıcı: mesaj 2", last)
        self.assertTrue(last.endswith("[Yeni mesaj]\nmesaj 3"))

    def test_history_char_budget_and_message_cap(self):
        bot, tg, ody = make_bot(history_turns=50, history_chars=1_000)
        for i in range(5):
            bot.handle_update(text_update(111, f"{i}-" + "x" * 400, update_id=i + 1))
        history = bot.state.chat(111)["history"]
        self.assertLessEqual(sum(len(t["u"]) + len(t["a"]) for t in history), 1_000)
        self.assertEqual(history[-1]["u"][:2], "4-")
        # Even an oversized stored history is cut to fit the API limit.
        chat = {"history": [{"u": "y" * 2000, "a": "z" * 3000}] * 20, "pending": []}
        message = bot.build_message(chat, "son")
        self.assertLessEqual(len(message), botmod.MAX_CHAT_MESSAGE)
        self.assertTrue(message.endswith("[Yeni mesaj]\nson"))

    def test_new_command_clears_context(self):
        bot, tg, ody = make_bot()
        bot.handle_update(text_update(111, "hatırla bunu"))
        bot.handle_update(text_update(111, "/yeni", update_id=2))
        bot.handle_update(text_update(111, "yeni konu", update_id=3))
        self.assertNotIn("hatırla bunu", ody.chat_messages()[-1])

    def test_odysseus_error_is_shown(self):
        bot, tg, ody = make_bot()

        def boom(*a, **k):
            raise botmod.OdysseusError(400, "Unsupported parameter: temperature")

        ody.request = boom
        bot.handle_update(text_update(111, "selam"))
        self.assertIn("Unsupported parameter: temperature", tg.sent[-1][1])

    def test_commands(self):
        bot, tg, ody = make_bot()
        bot.handle_update(text_update(111, "/yapilacaklar"))
        self.assertIn("Diş hekimi", tg.sent[-1][1])
        self.assertNotIn("**", tg.sent[-1][1])
        bot.handle_update(text_update(111, "/takvim 3"))
        self.assertIn("09.10 Cum 15:00 Toplantı", tg.sent[-1][1])
        cal = [c for c in ody.calls if c[1].startswith("/api/codex/calendar/events?")][0]
        self.assertIn("start=2026-10-06T00%3A00%3A00%2B03%3A00", cal[1])
        bot.handle_update(text_update(111, "/hafiza"))
        self.assertIn("Kahveyi şekersiz içer", tg.sent[-1][1])
        bot.handle_update(text_update(111, "/durum"))
        self.assertIn("gpt-5.6-sol", tg.sent[-1][1])
        bot.handle_update(text_update(111, "/yeni"))
        self.assertIn("Yeni sohbet", tg.sent[-1][1])
        bot.handle_update(text_update(111, "/kimim"))
        self.assertEqual(tg.sent[-1][1], "Chat ID: 111")
        self.assertEqual([c for c in ody.calls if c[1] == "/api/v1/chat"], [])

    def test_non_text_message(self):
        bot, tg, ody = make_bot()
        bot.handle_update({"update_id": 1, "message": {"chat": {"id": 111, "type": "private"}, "photo": []}})
        self.assertIn("metin", tg.sent[-1][1])


class ReminderEndpointTests(unittest.TestCase):
    def _serve(self, bot):
        server = botmod.ThreadingHTTPServer(("127.0.0.1", 0), botmod.make_http_handler(bot))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _post(self, port, body, headers=None, path="/reminder"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        data = json.dumps(body).encode()
        conn.request("POST", path, data, {"Content-Type": "application/json", **(headers or {})})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")

    def test_reminder_delivery_and_secret(self):
        bot, tg, _ = make_bot(allowed=(111, 222), reminder_secret="s3cret")
        port = self._serve(bot)
        status, _ = self._post(port, {"title": "Diş hekimi"})
        self.assertEqual(status, 401)
        status, _ = self._post(port, {"title": "Diş hekimi"}, {"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        status, body = self._post(
            port, {"title": "Diş hekimi", "message": "09:00 randevu"}, {"Authorization": "Bearer s3cret"}
        )
        self.assertEqual((status, body["delivered"]), (200, 2))
        self.assertEqual(tg.sent[-1], (222, "⏰ Diş hekimi\n\n09:00 randevu"))
        # Odysseus' placeholder template shape.
        self._post(port, {"content": "Su iç"}, {"Authorization": "Bearer s3cret"})
        self.assertEqual(tg.sent[-1], (222, "⏰ Su iç"))
        # GET (the integration form's Test button) honours the secret too.
        for headers, expected in (({}, 401), ({"Authorization": "Bearer s3cret"}, 200)):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/reminder", headers=headers)
            self.assertEqual(conn.getresponse().status, expected)

    def test_health_and_unconfigured(self):
        bot, _, _ = make_bot(allowed=())
        port = self._serve(bot)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.read())["pairing"])
        status, _ = self._post(port, {"title": "x"})
        self.assertEqual(status, 503)
        status, _ = self._post(port, {"title": "x"}, path="/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
