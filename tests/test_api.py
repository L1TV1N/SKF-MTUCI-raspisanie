"""Тесты сервера расписания: python3 -m unittest discover -s tests -v

Поднимают настоящий HTTP-сервер на временной базе, заполненной расписанием из data_src/.
"""
import datetime as dt
import http.cookiejar
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="raspisan-test-")
PASSWORD = secrets.token_urlsafe(12)
os.environ["RASPISAN_DB"] = str(Path(TMP) / "test.db")
os.environ["RASPISAN_ADMIN_PASSWORD"] = PASSWORD
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import server  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def raw(self, method, path, body=None, headers=None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if body is not None and not isinstance(body, bytes) and "Content-Type" not in (headers or {}):
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def json(self, method, path, body=None, headers=None):
        status, _, raw = self.raw(method, path, body, headers)
        return status, json.loads(raw) if raw else None

    def login(self):
        status, _ = self.json("POST", "/api/login", {"password": PASSWORD})
        assert status == 200


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = db.connect(os.environ["RASPISAN_DB"])
        db.init(conn)
        conn.close()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.c = Client(self.base)
        _, self.meta = self.c.json("GET", "/api/meta")

    def gid(self, name):
        return next(g["id"] for g in self.meta["groups"] if g["name"] == name)

    def tid(self, name):
        return next(t["id"] for t in self.meta["teachers"] if t["full_name"] == name)

    def rid(self, name):
        return next(r["id"] for r in self.meta["rooms"] if r["name"] == name)

    def sid(self, name):
        return next(s["id"] for s in self.meta["subjects"] if s["name"] == name)

    def week(self, **q):
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        status, data = self.c.json("GET", f"/api/schedule?{qs}")
        self.assertEqual(status, 200, data)
        return data

    def day(self, date, **q):
        return next(d for d in self.week(date=date, **q)["days"] if d["date"] == date)


# ─────────────────────────── данные ───────────────────────────

class TestSeed(Base):
    def test_counts(self):
        self.assertEqual(len(self.meta["groups"]), 19)
        self.assertEqual(self.meta["stats"]["lessons"], 1193)
        self.assertEqual(len(self.meta["bells"]), 7)
        self.assertEqual(self.meta["bells"][0], {"pair_no": 1, "starts_at": "08:30", "ends_at": "10:00"})

    def test_courses_from_group_code(self):
        course = {g["name"]: g["course"] for g in self.meta["groups"]}
        self.assertEqual(course["ДБ2601"], 1)
        self.assertEqual(course["ДМО2501"], 2)
        self.assertEqual(course["ДИ2401"], 3)
        self.assertEqual(course["ДП2302"], 4)

    def test_every_tsv_row_loaded(self):
        conn = self.enterContext(closing(sqlite3.connect(os.environ["RASPISAN_DB"])))
        for path in (ROOT / "data_src").glob("*.tsv"):
            rows = list(db.read_tsv(path))
            n = conn.execute("SELECT COUNT(*) FROM lessons l JOIN study_groups g ON g.id = l.group_id "
                             "WHERE g.name = ?", (path.stem,)).fetchone()[0]
            self.assertEqual(n, len(rows), path.stem)

    def test_normalization(self):
        names = {t["full_name"] for t in self.meta["teachers"]}
        self.assertIn("Ковалёв А.С.", names)
        self.assertNotIn("Ковалев А.С.", names)
        self.assertNotIn("Карпов Д.А", names)
        subjects = {s["name"] for s in self.meta["subjects"]}
        self.assertIn("Иностранный язык", subjects)
        self.assertFalse(any("п/г" in s for s in subjects))
        rooms = {r["name"]: r for r in self.meta["rooms"]}
        self.assertEqual(rooms["сп/зал"]["shared"], 1)
        self.assertEqual(rooms["Дистанционно"]["shared"], 1)
        self.assertEqual(rooms["402"]["shared"], 0)

    def test_only_known_source_conflicts(self):
        """Кроме 6 пересечений «Карпов Д.А.» из документов сайта, данные согласованы."""
        conn = self.enterContext(closing(db.connect(os.environ["RASPISAN_DB"])))
        slots = set()
        for les in conn.execute("SELECT * FROM lessons"):
            for cf in server.lesson_conflicts(conn, dict(les), les["id"]):
                self.assertIn("Карпов Д.А.", cf)
                slots.add((les["date"], les["pair_no"]))
        self.assertEqual(len(slots), 6)

    def test_no_reseed_after_all_groups_deleted(self):
        path = Path(TMP) / "reseed.db"
        conn = self.enterContext(closing(db.connect(path)))
        db.init(conn)
        conn.execute("DELETE FROM study_groups")
        conn.commit()
        db.init(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM study_groups").fetchone()[0], 0)

    def test_migration_from_first_version(self):
        """База первой версии (без date/assistant_id/shared/sem) обновляется без потерь."""
        path = Path(TMP) / "old.db"
        conn = sqlite3.connect(path)
        old = db.SCHEMA.replace(",'sem'", "").replace("""    note       TEXT NOT NULL DEFAULT '',
    date         TEXT,
    assistant_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL
);""", "    note       TEXT NOT NULL DEFAULT ''\n);").replace(
            ",\n    shared   INTEGER NOT NULL DEFAULT 0  -- 1: допускает параллельные занятия (спортзал, дистанционно)", "")
        self.assertNotIn("assistant_id", old)
        conn.executescript(old)
        conn.executescript("""
            INSERT INTO settings VALUES ('semester_start', '2026-09-01');
            INSERT INTO bells VALUES (1, '08:00', '09:30');
            INSERT INTO study_groups(name, course) VALUES ('Г-1', 1);
            INSERT INTO subjects(name) VALUES ('Матан');
            INSERT INTO lessons(group_id, weekday, pair_no, subject_id, kind) VALUES (1, 1, 1, 1, 'lab');""")
        conn.commit()
        conn.close()
        conn = self.enterContext(closing(db.connect(path)))
        db.init(conn)
        les = conn.execute("SELECT * FROM lessons").fetchone()
        self.assertEqual((les["kind"], les["date"], les["assistant_id"]), ("lab", None, None))
        conn.execute("INSERT INTO lessons(group_id, weekday, pair_no, subject_id, kind) VALUES (1, 2, 1, 1, 'sem')")
        conn.execute("INSERT INTO changes(date, group_id, pair_no, action, kind) VALUES ('2026-09-01', 1, 1, 'replace', 'sem')")
        self.assertEqual(conn.execute("SELECT shared FROM rooms").fetchall(), [])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM study_groups").fetchone()[0], 1)  # без повторного импорта


# ─────────────────────────── публичное API ───────────────────────────

class TestPublic(Base):
    def test_static_and_spa(self):
        for path, ctype in (("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css"),
                            ("/logo.svg", "image/svg+xml"), ("/group/5", "text/html"),
                            ("/admin", "text/html"), ("/admin/", "text/html")):
            status, headers, _ = self.c.raw("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype.split("/")[0], headers["Content-Type"], path)
        self.assertEqual(self.c.raw("GET", "/missing.js")[0], 404)
        status, headers, _ = self.c.raw("GET", "/favicon.ico")
        self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "image/svg+xml"))
        self.assertEqual(self.c.raw("HEAD", "/")[2], b"")

    def test_path_traversal(self):
        import socket
        host, port = self.httpd.server_address
        for path in ("/../server.py", "/%2e%2e/db.py", "/static/../server.py", "/..%2f..%2fetc/passwd"):
            with socket.create_connection((host, port)) as s:
                s.sendall(f"GET {path} HTTP/1.0\r\n\r\n".encode())
                resp = b""
                while chunk := s.recv(65536):
                    resp += chunk
            self.assertNotIn(b"import ", resp, path)
            self.assertNotIn(b"root:", resp, path)

    def test_meta(self):
        self.assertFalse(self.meta["admin"])
        self.assertIn(self.meta["week"]["parity"], ("odd", "even"))
        self.assertEqual(self.meta["today"], server.today_msk().isoformat())

    def test_group_week_matches_scan(self):
        """ДЗ2401, 22–24.09 — сверено со сканом."""
        g = self.gid("ДЗ2401")
        tue = self.day("2026-09-22", group=g)["items"]
        self.assertEqual([(i["pair_no"], i["subject"], i["kind"], i["room"]) for i in tue], [
            (4, "Инфокоммуникационные системы и сети", "prac", "216"),
            (5, "Сетевые технологии", "prac", "312"),
            (6, "Сетевые технологии", "lab", "312")])
        self.assertEqual(tue[2]["assistant"], "Манин А.А.")
        self.assertEqual(self.day("2026-09-24", group=g)["items"][1]["assistant"], "Панков Г.К.")

    def test_week_structure(self):
        w = self.week(group=self.gid("ДБ2601"), date="2026-09-17")
        self.assertEqual(w["monday"], "2026-09-14")
        self.assertEqual([d["date"] for d in w["days"]], [f"2026-09-{d}" for d in range(14, 20)])
        self.assertEqual(w["week_no"], 3)
        self.assertEqual(w["target"]["subtitle"], "1 курс · очная форма обучения")

    def test_subgroups_and_seminars(self):
        items = self.day("2026-09-16", group=self.gid("ДБ2601"))["items"]
        self.assertEqual([(i["pair_no"], i["subgroup"]) for i in items], [(1, 0), (2, 0), (3, 0), (4, 1)])
        sem = self.day("2026-09-15", group=self.gid("ДБ2601"))["items"][1]
        self.assertEqual((sem["subject"], sem["kind"], sem["room"]), ("История России", "sem", "306"))

    def test_stream_merged_in_teacher_view(self):
        items = self.day("2026-09-21", teacher=self.tid("Беркович В.Н."))["items"]
        lec = next(i for i in items if i["pair_no"] == 2)
        self.assertEqual(sorted(lec["groups"]), ["ДЗ2501", "ДЗ2502", "ДИ2501"])
        self.assertEqual(len(items), 3)

    def test_assistant_sees_own_lessons(self):
        items = self.day("2026-09-22", teacher=self.tid("Манин А.А."))["items"]
        self.assertTrue(any(i["assistant"] == "Манин А.А." and i["teacher"] == "Решетникова И.В." for i in items))

    def test_room_view_and_shared_room(self):
        items = self.day("2026-09-01", room=self.rid("сп/зал"))["items"]
        at1 = [i for i in items if i["pair_no"] == 1]
        self.assertGreaterEqual(len(at1), 2)  # несколько групп в спортзале одновременно
        w = self.week(room=self.rid("сп/зал"), date="2026-09-01")
        self.assertEqual(w["target"]["title"], "сп/зал")
        self.assertEqual(self.week(room=self.rid("402"), date="2026-09-01")["target"]["title"], "Аудитория 402")

    def test_empty_and_out_of_range_weeks(self):
        w = self.week(group=self.gid("ДБ2601"), date="2026-10-05")
        self.assertTrue(all(not d["items"] for d in w["days"]))
        self.week(group=self.gid("ДБ2601"), date="2020-01-01")

    def test_sunday_query_returns_its_week(self):
        w = self.week(group=self.gid("ДБ2601"), date="2026-09-20")
        self.assertEqual(w["monday"], "2026-09-14")

    def test_bad_requests(self):
        for path, code in (("/api/schedule", 400), ("/api/schedule?group=abc", 400), ("/api/schedule?group=0", 400),
                           ("/api/schedule?group=99999", 404), ("/api/schedule?teacher=99999", 404),
                           ("/api/schedule?group=1&date=2026-13-01", 400), ("/api/nope", 404),
                           ("/api/export.ics?room=99999", 404), ("/api/admin/groups", 401)):
            status, body = self.c.json("GET", path)
            self.assertEqual(status, code, path)
            self.assertIn("error", body)

    def test_ics(self):
        status, headers, raw = self.c.raw("GET", f"/api/export.ics?group={self.gid('ДБ2401')}")
        self.assertEqual(status, 200)
        self.assertIn("text/calendar", headers["Content-Type"])
        text = raw.decode()
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(text.count("BEGIN:VEVENT"), 42)
        self.assertEqual(text.count("BEGIN:VEVENT"), text.count("END:VEVENT"))
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75, line)
        self.assertIn("DTSTART;TZID=Europe/Moscow:20260901T115000", text)
        uids = [l for l in text.split("\r\n") if l.startswith("UID:")]
        self.assertEqual(len(uids), len(set(uids)))

    def test_ics_teacher_counts_streams_once(self):
        _, _, raw = self.c.raw("GET", f"/api/export.ics?teacher={self.tid('Беркович В.Н.')}")
        conn = self.enterContext(closing(sqlite3.connect(os.environ["RASPISAN_DB"])))
        slots = conn.execute("SELECT COUNT(DISTINCT date || '-' || pair_no) FROM lessons WHERE teacher_id = ?",
                             (self.tid("Беркович В.Н."),)).fetchone()[0]
        self.assertEqual(raw.decode().count("BEGIN:VEVENT"), slots)

    def test_ics_fold_multibyte(self):
        line = "SUMMARY:" + "Ж" * 100
        folded = server._ics_fold(line)
        self.assertEqual(folded.replace("\r\n ", ""), line)
        self.assertTrue(all(len(p.encode()) <= 75 for p in folded.split("\r\n")))


# ─────────────────────────── вход и администрирование ───────────────────────────

class TestAuth(Base):
    def test_login_logout(self):
        self.assertEqual(self.c.json("POST", "/api/login", {"password": "wrong"})[0], 401)
        self.assertEqual(self.c.json("GET", "/api/admin/teachers")[0], 401)
        self.c.login()
        self.assertTrue(self.c.json("GET", "/api/meta")[1]["admin"])
        self.assertEqual(self.c.json("GET", "/api/admin/teachers")[0], 200)
        self.c.json("POST", "/api/logout", {})
        self.assertEqual(self.c.json("GET", "/api/admin/teachers")[0], 401)

    def test_cookie_flags(self):
        _, headers, _ = self.c.raw("POST", "/api/login", {"password": PASSWORD})
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)

    def test_secure_cookie_behind_https_proxy(self):
        _, headers, _ = self.c.raw("POST", "/api/login", {"password": PASSWORD}, {"X-Forwarded-Proto": "https",
                                                                               "Content-Type": "application/json"})
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_forged_session_rejected(self):
        status, _ = self.c.json("GET", "/api/admin/teachers", headers={"Cookie": "sid=forged"})
        self.assertEqual(status, 401)

    def test_login_malformed(self):
        for body in (b"[1,2]", b"not json", b"\xff\xfe"):
            status, _, _ = self.c.raw("POST", "/api/login", body, {"Content-Type": "application/json"})
            self.assertEqual(status, 400, body)
        status, _, _ = self.c.raw("POST", "/api/login", b"password=x",
                                  {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)  # защита от CSRF через обычную форму


class TestAdmin(Base):
    def setUp(self):
        super().setUp()
        self.c.login()

    def post(self, entity, body, expect=201):
        status, data = self.c.json("POST", f"/api/admin/{entity}", body)
        self.assertEqual(status, expect, data)
        return data

    def put(self, path, body, expect=200):
        status, data = self.c.json("PUT", f"/api/admin/{path}", body)
        self.assertEqual(status, expect, data)
        return data

    def delete(self, path, expect=200):
        status, data = self.c.json("DELETE", f"/api/admin/{path}")
        self.assertEqual(status, expect, data)
        return data

    def test_reference_crud(self):
        g = self.post("groups", {"name": "ТЕСТ-1", "course": 2, "direction": "Тест"})["id"]
        self.post("groups", {"name": "ТЕСТ-1", "course": 2}, 409)
        self.post("groups", {"name": "", "course": 2}, 400)
        self.post("groups", {"name": "X", "course": 9}, 400)
        self.post("groups", {"name": "X", "course": True}, 400)
        self.post("groups", {"name": {"a": 1}, "course": 1}, 400)
        self.put(f"groups/{g}", {"direction": "Новое"})
        self.put(f"groups/{g}", {}, 400)
        self.put("groups/999999", {"name": "Z"}, 404)
        self.delete(f"groups/{g}")
        self.delete(f"groups/{g}", 404)

        r = self.post("rooms", {"name": "999", "capacity": 20, "shared": 1})["id"]
        self.post("rooms", {"name": "998", "capacity": -1}, 400)
        self.post("rooms", {"name": "998", "shared": 5}, 400)
        self.delete(f"rooms/{r}")
        self.post("teachers", {"full_name": "x" * 501}, 400)

    def test_subject_in_use_cannot_be_deleted(self):
        self.delete(f"subjects/{self.sid('Физика')}", 409)

    def test_lesson_validation(self):
        g = self.gid("ДБ2601")
        subj = self.sid("Физика")
        self.post("lessons", {"group_id": g, "pair_no": 1, "subject_id": subj}, 400)  # нет ни дня, ни даты
        self.post("lessons", {"group_id": g, "date": "2026-09-27", "pair_no": 1, "subject_id": subj}, 400)  # воскресенье
        self.post("lessons", {"group_id": g, "weekday": 7, "pair_no": 1, "subject_id": subj}, 400)
        self.post("lessons", {"group_id": g, "weekday": 1, "pair_no": 8, "subject_id": subj}, 400)  # нет 8-й пары
        self.post("lessons", {"group_id": g, "weekday": 1, "pair_no": 1, "subject_id": 999999}, 409)
        self.post("lessons", {"group_id": g, "weekday": 1, "pair_no": 1, "subject_id": subj, "kind": "x"}, 400)

    def test_dated_lesson_lifecycle_and_conflicts(self):
        g, subj, room = self.gid("ДБ2601"), self.sid("Физика"), self.rid("402")
        # 30.09, 1-я пара у ДБ2601 — «Физическая культура и спорт» в 217
        res = self.post("lessons", {"group_id": g, "date": "2026-09-30", "pair_no": 1, "subject_id": subj}, 409)
        self.assertTrue(any("ДБ2601" in c for c in res["conflicts"]))
        # 402 занята 30.09 на 2-й паре лекцией по истории у ДБ2601 — возьмём другую группу
        res = self.post("lessons", {"group_id": self.gid("ДП2301"), "date": "2026-09-30", "pair_no": 2,
                                    "subject_id": subj, "room_id": room}, 409)
        self.assertTrue(any("Аудитория 402" in c for c in res["conflicts"]))
        # Спортзал допускает параллельные занятия
        gym = self.post("lessons", {"group_id": self.gid("ДП2301"), "date": "2026-09-01", "pair_no": 1,
                                    "subject_id": subj, "room_id": self.rid("сп/зал")}, 201)["id"]
        self.delete(f"lessons/{gym}")
        # Ассистент тоже не может быть в двух местах
        res = self.post("lessons", {"group_id": self.gid("ДП2301"), "date": "2026-09-22", "pair_no": 6,
                                    "subject_id": subj, "teacher_id": self.tid("Манин А.А.")}, 409)
        self.assertTrue(any("Манин" in c for c in res["conflicts"]))
        # Свободный слот + принудительное сохранение
        new = self.post("lessons", {"group_id": g, "date": "2026-09-26", "pair_no": 5, "subject_id": subj,
                                    "kind": "sem", "note": "Тест"})["id"]
        item = self.day("2026-09-26", group=g)["items"][-1]
        self.assertEqual((item["pair_no"], item["kind"], item["note"]), (5, "sem", "Тест"))
        # Перенос на другую дату меняет и день недели
        self.put(f"lessons/{new}", {"date": "2026-10-05"})
        self.assertEqual(self.day("2026-10-05", group=g)["items"][0]["pair_no"], 5)
        self.assertFalse(any(i["pair_no"] == 5 for i in self.day("2026-09-26", group=g)["items"]))
        self.delete(f"lessons/{new}")

    def test_regular_lesson_parity(self):
        g, subj = self.gid("ДП2301"), self.sid("Физика")
        # Все даты сентября — 2026; 05.10 — понедельник 6-й недели (знаменатель)
        odd = self.post("lessons", {"group_id": g, "weekday": 1, "pair_no": 1, "subject_id": subj,
                                    "week_type": "odd"})["id"]
        even = self.post("lessons", {"group_id": g, "weekday": 1, "pair_no": 1, "subject_id": subj,
                                     "week_type": "even"})["id"]  # числитель/знаменатель не пересекаются
        self.post("lessons", {"group_id": g, "weekday": 1, "pair_no": 1, "subject_id": subj}, 409)
        self.assertEqual(len(self.day("2026-10-05", group=g)["items"]), 1)
        self.assertEqual(len(self.day("2026-10-12", group=g)["items"]), 1)
        # Разовое занятие ставит week_type = all даже при PUT
        self.put(f"lessons/{odd}", {"date": "2026-10-10", "force": True})
        conn = self.enterContext(closing(sqlite3.connect(os.environ["RASPISAN_DB"])))
        self.assertEqual(conn.execute("SELECT week_type, weekday FROM lessons WHERE id = ?", (odd,)).fetchone(),
                         ("all", 6))
        self.delete(f"lessons/{odd}")
        self.delete(f"lessons/{even}")

    def test_admin_lessons_list(self):
        status, rows = self.c.json("GET", f"/api/admin/lessons?group={self.gid('ДБ2401')}")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 42)
        self.assertEqual(self.c.json("GET", "/api/admin/lessons")[0], 400)

    def test_changes(self):
        g = self.gid("ДЗ2401")
        # 22.09: 4 пара отменена, 5-я — другая аудитория, 7-я — дополнительная
        c1 = self.post("changes", {"date": "2026-09-22", "group_id": g, "pair_no": 4, "action": "cancel",
                                   "note": "Болезнь"})["id"]
        c2 = self.post("changes", {"date": "2026-09-22", "group_id": g, "pair_no": 5, "action": "replace",
                                   "room_id": self.rid("402")})["id"]
        c3 = self.post("changes", {"date": "2026-09-22", "group_id": g, "pair_no": 7, "action": "replace",
                                   "subject_id": self.sid("Физика"), "kind": "lec"})["id"]
        self.post("changes", {"date": "2026-09-22", "group_id": g, "pair_no": 6, "action": "replace"}, 400)
        self.post("changes", {"date": "2026-09-27", "group_id": g, "pair_no": 1, "action": "cancel"}, 400)
        items = {i["pair_no"]: i for i in self.day("2026-09-22", group=g)["items"]}
        self.assertEqual((items[4]["status"], items[4]["change_note"]), ("cancelled", "Болезнь"))
        self.assertEqual((items[5]["status"], items[5]["room"], items[5]["orig"]["room"]), ("replaced", "402", "312"))
        self.assertEqual((items[7]["status"], items[7]["subject"]), ("added", "Физика"))
        # отменённая пара не попадает в календарь
        _, _, ics = self.c.raw("GET", f"/api/export.ics?group={g}")
        self.assertNotIn("Инфокоммуникационные системы и сети (практика)\r\nLOCATION:216", ics.decode())
        status, rows = self.c.json("GET", "/api/admin/changes?from=2026-09-22")
        self.assertEqual(status, 200)
        self.assertEqual(len([r for r in rows if r["group_id"] == g]), 3)
        for c in (c1, c2, c3):
            self.delete(f"changes/{c}")
        self.assertTrue(all(i["status"] == "normal" for i in self.day("2026-09-22", group=g)["items"]))

    def test_replace_teacher_drops_assistant(self):
        g = self.gid("ДЗ2401")
        c = self.post("changes", {"date": "2026-09-22", "group_id": g, "pair_no": 6, "action": "replace",
                                  "teacher_id": self.tid("Гуляева Т.В.")})["id"]
        item = next(i for i in self.day("2026-09-22", group=g)["items"] if i["pair_no"] == 6)
        self.assertEqual((item["teacher"], item["assistant"]), ("Гуляева Т.В.", None))
        self.delete(f"changes/{c}")

    def test_bells(self):
        _, meta = self.c.json("GET", "/api/meta")
        bells = meta["bells"]
        self.put("bells", [], 400)
        self.put("bells", {"a": 1}, 400)
        self.put("bells", [1, 2], 400)
        self.put("bells", [{**b, "starts_at": "25:00"} if b["pair_no"] == 1 else b for b in bells], 400)
        self.put("bells", bells + [dict(bells[0])], 400)  # дубль
        overlap = [dict(b) for b in bells]
        overlap[1]["starts_at"] = "09:50"  # раньше конца 1-й пары
        self.put("bells", overlap, 400)
        self.put("bells", bells[:6], 409)  # 7-я пара используется в расписании
        changed = [dict(b) for b in bells]
        changed[0]["starts_at"] = "08:00"
        self.put("bells", changed)
        self.assertEqual(self.c.json("GET", "/api/meta")[1]["bells"][0]["starts_at"], "08:00")
        self.put("bells", bells)

    def test_settings(self):
        self.put("settings", [], 400)
        self.put("settings", {"semester_start": "2026-13-01"}, 400)
        self.put("settings", {"first_week": "x"}, 400)
        self.put("settings", {"semester_end": "2026-01-01"}, 400)  # раньше начала
        self.put("settings", {"announcement": "x" * 3000}, 400)
        self.put("settings", {"show_parity": "1", "unknown": "ignored"})
        w = self.week(group=self.gid("ДБ2401"), date="2026-09-07")
        self.assertTrue(w["show_parity"])
        self.assertEqual((w["week_no"], w["parity"]), (2, "even"))
        self.put("settings", {"first_week": "even"})
        self.assertEqual(self.week(group=self.gid("ДБ2401"), date="2026-09-07")["parity"], "odd")
        self.put("settings", {"first_week": "odd", "show_parity": "0"})

    def test_huge_semester_does_not_hang(self):
        self.put("settings", {"semester_end": "2099-12-31"})
        status, _, raw = self.c.raw("GET", f"/api/export.ics?group={self.gid('ДБ2401')}")
        self.assertEqual(status, 200)
        self.put("settings", {"semester_end": "2026-12-31"})

    def test_cascade_delete_group(self):
        g = self.post("groups", {"name": "КАСКАД", "course": 1})["id"]
        self.post("lessons", {"group_id": g, "weekday": 6, "pair_no": 7, "subject_id": self.sid("Физика")})
        self.post("changes", {"date": "2026-09-26", "group_id": g, "pair_no": 7, "action": "cancel"})
        self.delete(f"groups/{g}")
        conn = self.enterContext(closing(sqlite3.connect(os.environ["RASPISAN_DB"])))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM lessons WHERE group_id = ?", (g,)).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM changes WHERE group_id = ?", (g,)).fetchone()[0], 0)

    def test_unknown_admin_routes(self):
        self.assertEqual(self.c.json("GET", "/api/admin/nope")[0], 404)
        self.assertEqual(self.c.json("GET", "/api/admin/groups/abc")[0], 400)
        self.assertEqual(self.c.json("PATCH", "/api/admin/groups/1")[0], 405)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
