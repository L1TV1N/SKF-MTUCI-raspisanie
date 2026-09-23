"""Расписание СКФ МТУСИ — HTTP-сервер (только стандартная библиотека Python).

Запуск:  python3 server.py            → http://localhost:8090
Переменные окружения:
  RASPISAN_PORT            порт (по умолчанию 8090)
  RASPISAN_HOST            адрес (по умолчанию 0.0.0.0)
  RASPISAN_DB              путь к файлу SQLite (по умолчанию data/raspisan.db)
  RASPISAN_ADMIN_PASSWORD  пароль администратора (по умолчанию admin — смените!)
  RASPISAN_NO_SEED=1       не заполнять пустую БД расписанием из data_src/
"""
import datetime as dt
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = os.environ.get("RASPISAN_DB", str(ROOT / "data" / "raspisan.db"))
ADMIN_PASSWORD = os.environ.get("RASPISAN_ADMIN_PASSWORD", "admin")
SESSION_TTL = 12 * 3600

MSK = dt.timezone(dt.timedelta(hours=3), "MSK")  # СКФ МТУСИ — Ростов-на-Дону, без перехода на летнее время
MAX_ICS_WEEKS = 60


def today_msk():
    return dt.datetime.now(MSK).date()


WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
KIND_LABEL = {"lec": "лекция", "prac": "практика", "lab": "лаб. работа", "sem": "семинар"}
PARITY_LABEL = {"odd": "Числитель", "even": "Знаменатель"}

_sessions = {}
_sessions_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


# ─────────────────────────── валидация ───────────────────────────

def _str(v, field):
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise ApiError(400, f"Поле «{field}»: ожидается строка")
    v = str(v).strip()
    if len(v) > 500:
        raise ApiError(400, f"Поле «{field}»: не длиннее 500 символов")
    return v


def _int(v, field, lo=None, hi=None, nullable=False):
    if v in (None, ""):
        if nullable:
            return None
        raise ApiError(400, f"Поле «{field}» обязательно")
    if isinstance(v, bool) or isinstance(v, float) and not v.is_integer():
        raise ApiError(400, f"Поле «{field}»: ожидается целое число")
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        raise ApiError(400, f"Поле «{field}»: ожидается число")
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise ApiError(400, f"Поле «{field}»: допустимо от {lo} до {hi}")
    return n


def _choice(options, nullable=False):
    def check(v, field):
        if v in (None, "") and nullable:
            return None
        if v not in options:
            raise ApiError(400, f"Поле «{field}»: недопустимое значение")
        return v
    return check


def _date(v, field):
    try:
        return dt.date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise ApiError(400, f"Поле «{field}»: ожидается дата ГГГГ-ММ-ДД")


def _date_opt(v, field):
    return None if v in (None, "") else _date(v, field)


def _ref(v, field):
    return _int(v, field, lo=1, nullable=True)


def _ref_req(v, field):
    return _int(v, field, lo=1)


# Разрешённые для записи таблицы: имя в API -> (таблица, поля, обязательные, сортировка)
ENTITIES = {
    "groups": ("study_groups", {
        "name": _str, "course": lambda v, f: _int(v, f, 1, 6), "direction": _str, "level": _str,
    }, ["name", "course"], "course, name"),
    "teachers": ("teachers", {
        "full_name": _str, "position": _str, "department": _str,
    }, ["full_name"], "full_name"),
    "rooms": ("rooms", {
        "name": _str, "building": _str, "capacity": lambda v, f: _int(v, f, 0, 10000, nullable=True), "kind": _str,
        "shared": lambda v, f: _int(v, f, 0, 1),
    }, ["name"], "name"),
    "subjects": ("subjects", {
        "name": _str, "short_name": _str,
    }, ["name"], "name"),
    "lessons": ("lessons", {
        "group_id": _ref_req, "weekday": lambda v, f: _int(v, f, 1, 6), "pair_no": lambda v, f: _int(v, f, 1, 8),
        "week_type": _choice({"all", "odd", "even"}), "subgroup": lambda v, f: _int(v, f, 0, 2),
        "subject_id": _ref_req, "kind": _choice({"lec", "prac", "lab", "sem"}),
        "teacher_id": _ref, "room_id": _ref, "note": _str,
        "date": _date_opt, "assistant_id": _ref,
    }, ["group_id", "pair_no", "subject_id"], "date, weekday, pair_no, subgroup"),
    "changes": ("changes", {
        "date": _date, "group_id": _ref_req, "pair_no": lambda v, f: _int(v, f, 1, 8),
        "subgroup": lambda v, f: _int(v, f, 0, 2), "action": _choice({"cancel", "replace"}),
        "subject_id": _ref, "kind": _choice({"lec", "prac", "lab", "sem"}, nullable=True),
        "teacher_id": _ref, "room_id": _ref, "note": _str,
    }, ["date", "group_id", "pair_no", "action"], "date, pair_no"),
}


def clean(entity, payload, partial=False):
    _, fields, required, _ = ENTITIES[entity]
    if not isinstance(payload, dict):
        raise ApiError(400, "Ожидается JSON-объект")
    out = {}
    for key, check in fields.items():
        if key in payload:
            out[key] = check(payload[key], key)
        elif not partial and key in required:
            raise ApiError(400, f"Поле «{key}» обязательно")
    for key in required:
        if key in out and out[key] in ("", None):
            raise ApiError(400, f"Поле «{key}» не может быть пустым")
    if entity == "lessons" and out.get("date"):
        wd = dt.date.fromisoformat(out["date"]).isoweekday()
        if wd == 7:
            raise ApiError(400, "Дата приходится на воскресенье")
        out["weekday"] = wd
    return out


# ─────────────────────────── недели и чётность ───────────────────────────

def settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def monday_of(d):
    return d - dt.timedelta(days=d.weekday())


def week_info(conn, monday):
    s = settings(conn)
    try:
        start = monday_of(dt.date.fromisoformat(s.get("semester_start", "")))
    except ValueError:
        start = monday
    week_no = (monday - start).days // 7 + 1
    odd = week_no % 2 == 1
    if s.get("first_week") == "even":
        odd = not odd
    parity = "odd" if odd else "even"
    return {"week_no": week_no, "parity": parity, "parity_label": PARITY_LABEL[parity],
            "show_parity": s.get("show_parity", "1") != "0"}


def date_parity(conn, date):
    return week_info(conn, monday_of(date))["parity"]


# ─────────────────────────── сборка расписания ───────────────────────────

LESSON_SQL = """
SELECT l.id, l.group_id, g.name AS group_name, l.weekday, l.pair_no, l.week_type, l.subgroup,
       l.kind, l.note, l.subject_id, s.name AS subject, s.short_name AS subject_short,
       l.teacher_id, t.full_name AS teacher, l.room_id, r.name AS room,
       l.date, l.assistant_id, a.full_name AS assistant, r.shared AS room_shared
FROM lessons l
JOIN study_groups g ON g.id = l.group_id
JOIN subjects s ON s.id = l.subject_id
LEFT JOIN teachers t ON t.id = l.teacher_id
LEFT JOIN teachers a ON a.id = l.assistant_id
LEFT JOIN rooms r ON r.id = l.room_id
"""

CHANGE_SQL = """
SELECT c.*, g.name AS group_name, s.name AS subject, s.short_name AS subject_short,
       t.full_name AS teacher, r.name AS room
FROM changes c
JOIN study_groups g ON g.id = c.group_id
LEFT JOIN subjects s ON s.id = c.subject_id
LEFT JOIN teachers t ON t.id = c.teacher_id
LEFT JOIN rooms r ON r.id = c.room_id
"""

ITEM_FIELDS = ("group_id", "group_name", "pair_no", "week_type", "subgroup", "kind", "note",
               "subject_id", "subject", "subject_short", "teacher_id", "teacher", "room_id", "room",
               "assistant_id", "assistant")


def _item(row, status="normal"):
    it = {k: row[k] for k in ITEM_FIELDS if k in row.keys()}
    it.setdefault("week_type", "all")
    it.setdefault("assistant_id", None)
    it.setdefault("assistant", None)
    it["status"] = status
    it["orig"] = None
    it["change_note"] = ""
    return it


def day_items(conn, date, parity):
    """Действующие занятия всех групп на дату с учётом замен."""
    wd = date.isoweekday()
    if wd > 6:
        return []
    base = [_item(r) for r in conn.execute(
        LESSON_SQL + " WHERE (l.date IS NULL AND l.weekday = ? AND l.week_type IN ('all', ?)) OR l.date = ?",
        (wd, parity, date.isoformat()))]
    for c in conn.execute(CHANGE_SQL + " WHERE c.date = ? ORDER BY c.id", (date.isoformat(),)):
        hits = [it for it in base if it["group_id"] == c["group_id"] and it["pair_no"] == c["pair_no"]
                and it["status"] != "cancelled"
                and (c["subgroup"] == 0 or it["subgroup"] in (0, c["subgroup"]))]
        if c["action"] == "cancel":
            for it in hits:
                it["status"], it["change_note"] = "cancelled", c["note"]
            continue
        if hits:
            for it in hits:
                it["orig"] = {k: it[k] for k in ("subject", "teacher", "room", "kind")}
                if c["teacher_id"] is not None:
                    it["assistant_id"] = it["assistant"] = None
                for k in ("subject_id", "subject", "subject_short", "kind", "teacher_id", "teacher", "room_id", "room"):
                    if c[k] is not None:
                        it[k] = c[k]
                it["status"], it["change_note"] = "replaced", c["note"]
        elif c["subject_id"] is not None:
            it = _item(c, "added")
            it["kind"] = c["kind"] or "prac"
            it["change_note"] = c["note"]
            base.append(it)
    return base


def _merge_streams(items):
    """Поточные занятия (одна пара у нескольких групп) показываем одной карточкой."""
    merged = {}
    for it in items:
        key = (it["pair_no"], it["subject_id"], it["teacher_id"], it["assistant_id"], it["room_id"], it["kind"],
               it["status"], it["subgroup"], it["week_type"])
        if key in merged:
            merged[key]["groups"].append(it["group_name"])
        else:
            merged[key] = {**it, "groups": [it["group_name"]]}
    return sorted(merged.values(), key=lambda x: (x["pair_no"], x["subgroup"], x["groups"]))


def target_info(conn, kind, tid):
    if kind == "group":
        r = conn.execute("SELECT * FROM study_groups WHERE id = ?", (tid,)).fetchone()
        if r:
            return {"kind": kind, "id": tid, "title": r["name"],
                    "subtitle": f"{r['course']} курс · {r['direction']}".strip(" ·")}
    elif kind == "teacher":
        r = conn.execute("SELECT * FROM teachers WHERE id = ?", (tid,)).fetchone()
        if r:
            return {"kind": kind, "id": tid, "title": r["full_name"],
                    "subtitle": " · ".join(x for x in (r["position"], r["department"]) if x)}
    elif kind == "room":
        r = conn.execute("SELECT * FROM rooms WHERE id = ?", (tid,)).fetchone()
        if r:
            cap = f"{r['capacity']} мест" if r["capacity"] else ""
            return {"kind": kind, "id": tid, "title": f"Аудитория {r['name']}" if r["name"][:1].isdigit() else r["name"],
                    "subtitle": " · ".join(x for x in (r["kind"], r["building"], cap) if x)}
    raise ApiError(404, "Не найдено")


def _filter(items, kind, tid):
    if kind == "teacher":  # преподаватель видит и пары, где он ассистент
        return [it for it in items if tid in (it["teacher_id"], it["assistant_id"])]
    key = {"group": "group_id", "room": "room_id"}[kind]
    return [it for it in items if it[key] == tid]


def week_schedule(conn, kind, tid, monday):
    target = target_info(conn, kind, tid)
    info = week_info(conn, monday)
    days = []
    for i in range(6):
        date = monday + dt.timedelta(days=i)
        items = _merge_streams(_filter(day_items(conn, date, info["parity"]), kind, tid))
        days.append({"date": date.isoformat(), "weekday": i + 1, "title": WEEKDAYS[i], "items": items})
    return {"monday": monday.isoformat(), **info, "target": target, "days": days}


# ─────────────────────────── конфликты ───────────────────────────

def _overlap_weeks(a, b):
    return a == "all" or b == "all" or a == b


def _overlap_subs(a, b):
    return a == 0 or b == 0 or a == b


def lesson_conflicts(conn, les, exclude_id=None):
    """Пересечения по группе, преподавателю/ассистенту и аудитории.
    Поток (одна дисциплина, вид, преподаватель и аудитория у разных групп) — не пересечение."""
    les_date = dt.date.fromisoformat(les["date"]) if les.get("date") else None
    rows = conn.execute(
        LESSON_SQL + " WHERE l.weekday = ? AND l.pair_no = ? AND l.id IS NOT ?",
        (les["weekday"], les["pair_no"], exclude_id)).fetchall()
    out = []
    for o in rows:
        if les_date and o["date"]:
            if o["date"] != les["date"]:
                continue
        elif les_date:  # разовое против регулярного
            if not _overlap_weeks(o["week_type"], date_parity(conn, les_date)):
                continue
        elif o["date"]:  # регулярное против разового
            if not _overlap_weeks(les["week_type"], date_parity(conn, dt.date.fromisoformat(o["date"]))):
                continue
        elif not _overlap_weeks(les["week_type"], o["week_type"]):
            continue
        when = f", {dt.date.fromisoformat(o['date']).strftime('%d.%m')}" if o["date"] else ""
        what = f"{o['subject']} ({KIND_LABEL[o['kind']]}, {o['group_name']}{when})"
        same_group = o["group_id"] == les["group_id"]
        if same_group and _overlap_subs(les["subgroup"], o["subgroup"]):
            out.append(f"У группы {o['group_name']} в это время уже стоит: {what}")
            continue
        stream = (not same_group and les["kind"] == o["kind"] and les["subject_id"] == o["subject_id"]
                  and les["teacher_id"] == o["teacher_id"] and les["room_id"] == o["room_id"])
        if stream:
            continue
        mine = {les.get("teacher_id"), les.get("assistant_id")} - {None}
        theirs = {o["teacher_id"], o["assistant_id"]} - {None}
        for tid in mine & theirs:
            name = o["teacher"] if tid == o["teacher_id"] else o["assistant"]
            out.append(f"Преподаватель {name} занят: {what}")
        if les.get("room_id") and les["room_id"] == o["room_id"] and not o["room_shared"]:
            out.append(f"Аудитория {o['room']} занята: {what}")
    return out


# ─────────────────────────── iCalendar ───────────────────────────

def _ics_escape(s):
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line):
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def build_ics(conn, kind, tid):
    s = settings(conn)
    info = target_info(conn, kind, tid)
    bells = {r["pair_no"]: (r["starts_at"], r["ends_at"]) for r in conn.execute("SELECT * FROM bells")}
    try:
        start = dt.date.fromisoformat(s["semester_start"])
        end = dt.date.fromisoformat(s["semester_end"])
    except (KeyError, ValueError):
        start = today_msk()
        end = start + dt.timedelta(weeks=18)
    end = min(end, start + dt.timedelta(weeks=MAX_ICS_WEEKS))  # защита от «семестра» в несколько лет
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SKF MTUCI//Raspisanie//RU", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{_ics_escape(info['title'])}", "X-WR-TIMEZONE:Europe/Moscow",
             "BEGIN:VTIMEZONE", "TZID:Europe/Moscow", "BEGIN:STANDARD", "DTSTART:19700101T000000",
             "TZOFFSETFROM:+0300", "TZOFFSETTO:+0300", "TZNAME:MSK", "END:STANDARD", "END:VTIMEZONE"]
    monday = monday_of(start)
    while monday <= end:
        parity = week_info(conn, monday)["parity"]
        for i in range(6):
            date = monday + dt.timedelta(days=i)
            if date < start or date > end:
                continue
            for it in _merge_streams(_filter(day_items(conn, date, parity), kind, tid)):
                if it["status"] == "cancelled" or it["pair_no"] not in bells:
                    continue
                b, e = bells[it["pair_no"]]
                summary = f"{it['subject']} ({KIND_LABEL.get(it['kind'], '')})"
                if it["subgroup"]:
                    summary += f" · подгр. {it['subgroup']}"
                desc = [f"{it['pair_no']} пара", ", ".join(it["groups"])]
                if it["teacher"]:
                    desc.append(it["teacher"])
                if it["assistant"]:
                    desc.append("Ассистент: " + it["assistant"])
                if it["change_note"]:
                    desc.append("Замена: " + it["change_note"])
                uid_src = f"{kind}{tid}{date}{it['pair_no']}{it['subgroup']}{it['subject_id']}{','.join(it['groups'])}"
                uid = hashlib.sha1(uid_src.encode()).hexdigest()[:20]
                d = date.strftime("%Y%m%d")
                lines += ["BEGIN:VEVENT", f"UID:{uid}@raspisan.skf-mtuci", f"DTSTAMP:{stamp}",
                          f"DTSTART;TZID=Europe/Moscow:{d}T{b.replace(':', '')}00",
                          f"DTEND;TZID=Europe/Moscow:{d}T{e.replace(':', '')}00",
                          f"SUMMARY:{_ics_escape(summary)}",
                          f"LOCATION:{_ics_escape(it['room'] or '')}",
                          f"DESCRIPTION:{_ics_escape(chr(10).join(desc))}", "END:VEVENT"]
        monday += dt.timedelta(days=7)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(l) for l in lines) + "\r\n"


# ─────────────────────────── HTTP ───────────────────────────

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Handler(BaseHTTPRequestHandler):
    server_version = "Raspisan/1.0"

    def log_message(self, fmt, *args):
        # За nginx настоящий адрес посетителя приходит в X-Real-IP
        headers = getattr(self, "headers", None)  # при битом запросе заголовков может не быть
        client = (headers and headers.get("X-Real-IP")) or self.address_string()
        print(f"[{self.log_date_time_string()}] {client} {fmt % args}")

    def _cookie(self, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"sid={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}"

    # — ответы —
    def _send(self, status, body, ctype, extra_headers=None):
        if getattr(self, "_deferred", None) is not None:
            # Ответ API отдаём только после commit, иначе клиент может прочитать старые данные
            self._deferred = (status, body, ctype, extra_headers)
            return
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, obj, headers=None):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store", **(headers or {})})

    def _body(self, expect=dict):
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ApiError(415, "Ожидается Content-Type: application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Некорректный Content-Length")
        if n < 0 or n > 1_000_000:
            raise ApiError(413, "Слишком большой запрос")
        try:
            body = json.loads(self.rfile.read(n) or (b"[]" if expect is list else b"{}"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "Некорректный JSON")
        if not isinstance(body, expect):
            raise ApiError(400, "Ожидается JSON-" + ("список" if expect is list else "объект"))
        return body

    # — сессии —
    def _sid(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid":
                return v
        return None

    def _is_admin(self):
        sid = self._sid()
        if not sid:
            return False
        with _sessions_lock:
            exp = _sessions.get(sid)
            if exp and exp > time.time():
                return True
            _sessions.pop(sid, None)
        return False

    # — маршрутизация —
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._json(405, {"error": "Метод не поддерживается"}, {"Allow": "GET, HEAD, POST, PUT, DELETE"})

    do_OPTIONS = do_PATCH

    def _dispatch(self, method):
        url = urlparse(self.path)
        path = url.path
        if not path.startswith("/api/"):
            if method in ("GET", "HEAD"):
                return self._static(path)
            return self._json(405, {"error": "Метод не поддерживается"})
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        conn = db.connect(DB_PATH)
        self._deferred = False
        try:
            self._api(method, path, q, conn)
            conn.commit()
            response, self._deferred = self._deferred, None
            if response:
                self._send(*response)
        except ApiError as e:
            conn.rollback()
            self._deferred = None
            self._json(e.status, {"error": e.message, **e.extra})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            self._deferred = None
            msg = str(e)
            if "UNIQUE" in msg:
                text = "Такая запись уже существует"
            elif "FOREIGN KEY" in msg:
                text = "Запись связана с другими данными (например, с занятиями в расписании)"
            else:
                text = "Нарушены ограничения данных"
            self._json(409, {"error": text})
        except Exception as e:  # noqa: BLE001 — не отдаём трейсбек клиенту
            conn.rollback()
            self._deferred = None
            print("Ошибка:", repr(e))
            self._json(500, {"error": "Внутренняя ошибка сервера"})
        finally:
            self._deferred = None
            conn.close()

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if rel == "favicon.ico":  # браузеры запрашивают его независимо от <link rel="icon">
            rel = "logo.svg"
        target = (STATIC / rel).resolve()
        if not target.is_relative_to(STATIC) or not target.is_file():
            if Path(rel).suffix:
                return self._send(404, "Not found", "text/plain; charset=utf-8")
            target = STATIC / "index.html"  # SPA-маршрутизация
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def _api(self, method, path, q, conn):
        parts = path.strip("/").split("/")[1:]  # без "api"

        # ——— публичное ———
        if method == "GET" and parts == ["meta"]:
            today = today_msk()
            return self._json(200, {
                "settings": settings(conn),
                "bells": [dict(r) for r in conn.execute("SELECT * FROM bells ORDER BY pair_no")],
                "groups": [dict(r) for r in conn.execute("SELECT * FROM study_groups ORDER BY course, name")],
                "teachers": [dict(r) for r in conn.execute("SELECT * FROM teachers ORDER BY full_name")],
                "rooms": [dict(r) for r in conn.execute("SELECT * FROM rooms ORDER BY name")],
                "subjects": [dict(r) for r in conn.execute("SELECT * FROM subjects ORDER BY name")],
                "stats": {"lessons": conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]},
                "today": today.isoformat(),
                "week": week_info(conn, monday_of(today)),
                "admin": self._is_admin(),
            })

        if method == "GET" and parts == ["schedule"]:
            kind, tid = self._target(q)
            try:
                d = dt.date.fromisoformat(q["date"]) if q.get("date") else today_msk()
            except ValueError:
                raise ApiError(400, "Некорректная дата")
            return self._json(200, week_schedule(conn, kind, tid, monday_of(d)))

        if method == "GET" and parts == ["export.ics"]:
            kind, tid = self._target(q)
            body = build_ics(conn, kind, tid)
            return self._send(200, body, "text/calendar; charset=utf-8", {
                "Content-Disposition": f'attachment; filename="raspisanie-{kind}-{tid}.ics"'})

        if method == "POST" and parts == ["login"]:
            body = self._body()
            pw = str(body.get("password", ""))
            if not hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
                time.sleep(0.7)
                raise ApiError(401, "Неверный пароль")
            sid = secrets.token_urlsafe(32)
            now = time.time()
            with _sessions_lock:
                for old in [k for k, exp in _sessions.items() if exp <= now]:
                    del _sessions[old]
                _sessions[sid] = now + SESSION_TTL
            return self._json(200, {"ok": True}, {
                "Set-Cookie": self._cookie(sid, SESSION_TTL)})

        if method == "POST" and parts == ["logout"]:
            sid = self._sid()
            with _sessions_lock:
                _sessions.pop(sid, None)
            return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})

        # ——— администрирование ———
        if parts[:1] == ["admin"]:
            if not self._is_admin():
                raise ApiError(401, "Требуется вход администратора")
            return self._admin(method, parts[1:], q, conn)

        raise ApiError(404, "Не найдено")

    def _target(self, q):
        for kind in ("group", "teacher", "room"):
            if q.get(kind):
                return kind, _int(q[kind], kind, lo=1)
        raise ApiError(400, "Укажите group, teacher или room")

    def _admin(self, method, parts, q, conn):
        if parts == ["bells"] and method == "PUT":
            rows = self._body(list)
            if not rows:
                raise ApiError(400, "Ожидается список пар")
            seen = set()
            for r in rows:
                if not isinstance(r, dict):
                    raise ApiError(400, "Ожидается список объектов {pair_no, starts_at, ends_at}")
                n = _int(r.get("pair_no"), "pair_no", 1, 8)
                if n in seen:
                    raise ApiError(400, f"Пара №{n} указана дважды")
                a, b = str(r.get("starts_at", "")), str(r.get("ends_at", ""))
                if not TIME_RE.match(a) or not TIME_RE.match(b) or a >= b:
                    raise ApiError(400, f"Пара {n}: время должно быть ЧЧ:ММ, начало раньше конца")
                seen.add(n)
                conn.execute("INSERT INTO bells VALUES (?, ?, ?) ON CONFLICT(pair_no) DO UPDATE "
                             "SET starts_at = excluded.starts_at, ends_at = excluded.ends_at", (n, a, b))
            times = sorted((int(r["pair_no"]), r["starts_at"], r["ends_at"]) for r in rows)
            for (n1, _, e1), (n2, s2, _) in zip(times, times[1:]):
                if s2 < e1:
                    raise ApiError(400, f"Пара {n2} начинается раньше, чем заканчивается пара {n1}")
            ph = ",".join("?" * len(seen))
            conn.execute(f"DELETE FROM bells WHERE pair_no NOT IN ({ph})", tuple(seen))
            return self._json(200, {"ok": True})

        if parts == ["settings"] and method == "PUT":
            body = self._body()
            allowed = {"semester_title", "semester_start", "semester_end", "first_week", "show_parity", "announcement"}
            merged = {**settings(conn), **{k: str(v).strip() for k, v in body.items() if k in allowed}}
            if merged.get("semester_start") and merged.get("semester_end"):
                if _date(merged["semester_end"], "semester_end") < _date(merged["semester_start"], "semester_start"):
                    raise ApiError(400, "Конец семестра раньше начала")
            for k, v in body.items():
                if k not in allowed:
                    continue
                v = str(v).strip()
                if k in ("semester_start", "semester_end"):
                    v = _date(v, k)
                if k == "first_week" and v not in ("odd", "even"):
                    raise ApiError(400, "first_week: odd или even")
                if k == "show_parity" and v not in ("0", "1"):
                    raise ApiError(400, "show_parity: 0 или 1")
                if len(v) > 2000:
                    raise ApiError(400, f"{k}: слишком длинное значение")
                conn.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                             (k, v))
            return self._json(200, {"ok": True})

        if not parts or parts[0] not in ENTITIES:
            raise ApiError(404, "Не найдено")
        entity = parts[0]
        table, _, _, order = ENTITIES[entity]
        item_id = _int(parts[1], "id", lo=1) if len(parts) > 1 else None

        if method == "GET" and item_id is None:
            if entity == "lessons":
                gid = _int(q.get("group"), "group", lo=1)
                rows = conn.execute(LESSON_SQL + " WHERE l.group_id = ?"
                                    " ORDER BY l.date IS NOT NULL, l.date, l.weekday, l.pair_no, l.subgroup, l.week_type",
                                    (gid,)).fetchall()
            elif entity == "changes":
                since = _date(q.get("from") or today_msk().isoformat(), "from")
                rows = conn.execute(CHANGE_SQL + " WHERE c.date >= ? ORDER BY c.date, c.pair_no", (since,)).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            return self._json(200, [dict(r) for r in rows])

        if method == "POST" and item_id is None:
            body = self._body()
            data = clean(entity, body)
            self._validate(entity, data, body, conn, None)
            cols = ", ".join(data)
            cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(data))})",
                               tuple(data.values()))
            return self._json(201, {"id": cur.lastrowid})

        if method == "PUT" and item_id:
            body = self._body()
            data = clean(entity, body, partial=True)
            if not data:
                raise ApiError(400, "Нечего сохранять")
            current = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
            if not current:
                raise ApiError(404, "Запись не найдена")
            merged = {**dict(current), **data}
            self._validate(entity, merged, body, conn, item_id)
            if entity == "lessons":
                data["week_type"] = merged["week_type"]
            sets = ", ".join(f"{k} = ?" for k in data)
            conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*data.values(), item_id))
            return self._json(200, {"ok": True})

        if method == "DELETE" and item_id:
            cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
            if not cur.rowcount:
                raise ApiError(404, "Запись не найдена")
            return self._json(200, {"ok": True})

        raise ApiError(405, "Метод не поддерживается")

    def _validate(self, entity, data, body, conn, item_id):
        if entity in ("lessons", "changes"):
            if not conn.execute("SELECT 1 FROM bells WHERE pair_no = ?", (data["pair_no"],)).fetchone():
                raise ApiError(400, f"Пары №{data['pair_no']} нет в расписании звонков")
        if entity == "lessons":
            if not data.get("weekday"):
                raise ApiError(400, "Укажите день недели или дату занятия")
            if data.get("date"):
                data["week_type"] = "all"
            data.setdefault("assistant_id", None)
            data.setdefault("date", None)
            data.setdefault("week_type", "all")
            data.setdefault("subgroup", 0)
            data.setdefault("kind", "lec")
            data.setdefault("teacher_id", None)
            data.setdefault("room_id", None)
            conflicts = lesson_conflicts(conn, data, item_id)
            if conflicts and not body.get("force"):
                raise ApiError(409, "Найдены пересечения в расписании", {"conflicts": conflicts})
        if entity == "changes" and dt.date.fromisoformat(data["date"]).isoweekday() == 7:
            raise ApiError(400, "Дата приходится на воскресенье")
        if entity == "changes" and data["action"] == "replace":
            if not any(data.get(k) for k in ("subject_id", "teacher_id", "room_id")) and not data.get("note"):
                raise ApiError(400, "Для замены укажите, что меняется: дисциплину, преподавателя или аудиторию")


def main():
    sys.stdout.reconfigure(line_buffering=True)  # журнал сразу виден в systemd/docker/файле
    conn = db.connect(DB_PATH)
    db.init(conn, seed=os.environ.get("RASPISAN_NO_SEED") != "1")
    conn.close()
    host = os.environ.get("RASPISAN_HOST", "0.0.0.0")
    port = int(os.environ.get("RASPISAN_PORT", "8090"))
    if ADMIN_PASSWORD == "admin":
        print("⚠  Пароль администратора по умолчанию «admin». Задайте RASPISAN_ADMIN_PASSWORD.")
    print(f"Расписание СКФ МТУСИ: http://localhost:{port}  (БД: {DB_PATH})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
