"""База данных расписания СКФ МТУСИ: схема SQLite и загрузка реального расписания."""
import csv
import re
import datetime as dt
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Расписание звонков: номер пары -> время
CREATE TABLE IF NOT EXISTS bells (
    pair_no   INTEGER PRIMARY KEY CHECK (pair_no BETWEEN 1 AND 8),
    starts_at TEXT NOT NULL,
    ends_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_groups (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    course    INTEGER NOT NULL CHECK (course BETWEEN 1 AND 6),
    direction TEXT NOT NULL DEFAULT '',
    level     TEXT NOT NULL DEFAULT 'бакалавриат'
);

CREATE TABLE IF NOT EXISTS teachers (
    id         INTEGER PRIMARY KEY,
    full_name  TEXT NOT NULL UNIQUE,
    position   TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS subjects (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    short_name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rooms (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    building TEXT NOT NULL DEFAULT '',
    capacity INTEGER,
    kind     TEXT NOT NULL DEFAULT '',
    shared   INTEGER NOT NULL DEFAULT 0  -- 1: допускает параллельные занятия (спортзал, дистанционно)
);

-- Занятия. Если date задана — разовое занятие в эту дату (так публикует
-- расписание СКФ МТУСИ: помесячно, по датам). Если date пуста — регулярное:
-- week_type all — каждую неделю, odd — числитель, even — знаменатель.
-- subgroup: 0 — вся группа, 1/2 — подгруппа.
CREATE TABLE IF NOT EXISTS lessons (
    id         INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 6),
    pair_no    INTEGER NOT NULL REFERENCES bells(pair_no) ON DELETE RESTRICT,
    week_type  TEXT NOT NULL DEFAULT 'all' CHECK (week_type IN ('all','odd','even')),
    subgroup   INTEGER NOT NULL DEFAULT 0 CHECK (subgroup IN (0,1,2)),
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE RESTRICT,
    kind       TEXT NOT NULL DEFAULT 'lec' CHECK (kind IN ('lec','prac','lab','sem')),
    teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    room_id    INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    note       TEXT NOT NULL DEFAULT '',
    date         TEXT,
    assistant_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_lessons_slot    ON lessons(weekday, pair_no);
CREATE INDEX IF NOT EXISTS ix_lessons_group   ON lessons(group_id);
CREATE INDEX IF NOT EXISTS ix_lessons_teacher ON lessons(teacher_id);
CREATE INDEX IF NOT EXISTS ix_lessons_room    ON lessons(room_id);

-- Замены и отмены на конкретную дату.
-- cancel — пара отменена; replace — пара заменена (или добавлена, если в слоте пусто).
CREATE TABLE IF NOT EXISTS changes (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    group_id   INTEGER NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
    pair_no    INTEGER NOT NULL REFERENCES bells(pair_no) ON DELETE RESTRICT,
    subgroup   INTEGER NOT NULL DEFAULT 0 CHECK (subgroup IN (0,1,2)),
    action     TEXT NOT NULL CHECK (action IN ('cancel','replace')),
    subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
    kind       TEXT CHECK (kind IS NULL OR kind IN ('lec','prac','lab','sem')),
    teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    room_id    INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_changes_date ON changes(date);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _widen_kind_check(conn, table):
    """Старые базы не знали вид «семинар» (sem): пересоздаём таблицу с новым CHECK."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    if not sql or "'sem'" in sql[0]:
        return
    new_sql = sql[0].replace("'lab')", "'lab','sem')").replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}__new", 1)
    new_sql = new_sql.replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {table}__new", 1)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(f"BEGIN; {new_sql}; INSERT INTO {table}__new SELECT * FROM {table}; "
                       f"DROP TABLE {table}; ALTER TABLE {table}__new RENAME TO {table}; COMMIT;")
    conn.execute("PRAGMA foreign_keys = ON")


def init(conn, seed=True):
    conn.executescript(SCHEMA)
    # Миграция баз, созданных до появления разовых занятий и ассистентов
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(lessons)")}
    if "date" not in cols:
        conn.execute("ALTER TABLE lessons ADD COLUMN date TEXT")
    if "assistant_id" not in cols:
        conn.execute("ALTER TABLE lessons ADD COLUMN assistant_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL")
    if "shared" not in {r["name"] for r in conn.execute("PRAGMA table_info(rooms)")}:
        conn.execute("ALTER TABLE rooms ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
    for table in ("lessons", "changes"):
        _widen_kind_check(conn, table)
    conn.executescript(SCHEMA)  # вернуть индексы после пересоздания таблиц
    conn.execute("CREATE INDEX IF NOT EXISTS ix_lessons_date ON lessons(date)")
    # Заполняем только новую базу: если администратор удалил все группы, данные не должны вернуться
    fresh = conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    if seed and fresh:
        seed_real(conn)
    conn.commit()


# ─────────────────────────── реальные данные СКФ МТУСИ ───────────────────────────
# Источник: https://skf.mtuci.ru/uch_rab/time-table/ (Высшее → Очная → Третий курс → Занятия)
# и «Расписание звонков для студентов очной и заочной формы обучения» там же.
# Расписания групп опубликованы сканами PDF; они вручную перенесены в data_src/*.tsv.

SOURCE_URL = "https://skf.mtuci.ru/uch_rab/time-table/"
DATA_SRC = Path(__file__).resolve().parent / "data_src"
YEAR = 2026

BELLS = [(1, "08:30", "10:00"), (2, "10:10", "11:40"), (3, "11:50", "13:20"),
         (4, "13:50", "15:20"), (5, "15:30", "17:00"), (6, "17:10", "18:40"),
         (7, "18:45", "20:15")]

KINDS = {"лек.": "lec", "ПЗ": "prac", "ЛР": "lab", "Семин.": "sem"}
# аудитория в скане -> (название в базе, тип)
ROOMS = {"сп/зал": ("сп/зал", "Спортивный зал", 1), "дистанционно": ("Дистанционно", "Дистанционное занятие", 1)}
SUBGROUP_RE = re.compile(r"\s*(?:(\d)\s*п/г|п/г\s*(\d))\s*$")
# Опечатки и разные написания одного человека в сканах
PEOPLE = {"Ковалев А.С.": "Ковалёв А.С.", "Карпов Д.А": "Карпов Д.А."}


def read_tsv(path):
    """Строки расписания группы: дата, пара, дисциплина, преподаватель, ассистент, аудитория, вид."""
    with open(path, encoding="utf-8") as f:
        for n, row in enumerate(csv.reader(f, delimiter="\t"), 1):
            if not row or row[0].startswith("#"):
                continue
            if len(row) != 7:
                raise ValueError(f"{path.name}:{n}: ожидается 7 колонок, получено {len(row)}")
            day, pair, subject, teacher, assistant, room, kind = (c.strip() for c in row)
            d, m = map(int, day.split("."))
            sub = SUBGROUP_RE.search(subject)  # «Иностранный язык 1 п/г» -> подгруппа 1
            yield {"date": f"{YEAR}-{m:02d}-{d:02d}", "pair": int(pair),
                   "subject": SUBGROUP_RE.sub("", subject) if sub else subject,
                   "subgroup": int(sub.group(1) or sub.group(2)) if sub else 0,
                   "teacher": PEOPLE.get(teacher, teacher), "assistant": PEOPLE.get(assistant, assistant),
                   "room": room, "kind": KINDS[kind]}


def seed_real(conn):
    conn.executemany("INSERT INTO settings(key, value) VALUES (?, ?)", [
        ("semester_title", "Осенний семестр 2026/27"),
        ("semester_start", "2026-09-01"),
        ("semester_end", "2026-12-31"),
        ("first_week", "odd"),
        ("show_parity", "0"),
        ("announcement", "Расписание 1–4 курсов очной формы на сентябрь 2026 г. перенесено с официального сайта "
                         f"СКФ МТУСИ ({SOURCE_URL}). При расхождениях верным считается документ на сайте."),
    ])
    conn.executemany("INSERT INTO bells VALUES (?, ?, ?)", BELLS)

    ids = {"group": {}, "teacher": {}, "subject": {}, "room": {}}

    def ref(kind, name, insert):
        if not name:
            return None
        if name not in ids[kind]:
            ids[kind][name] = conn.execute(*insert(name)).lastrowid
        return ids[kind][name]

    for path in sorted(DATA_SRC.glob("*.tsv")):
        intake = 2000 + int(re.search(r"(\d{2})\d{2}$", path.stem).group(1))  # ДБ2401 -> набор 2024
        gid = ref("group", path.stem, lambda n: (
            "INSERT INTO study_groups(name, course, direction, level) VALUES (?, ?, 'очная форма обучения', 'бакалавриат')",
            (n, YEAR - intake + 1)))
        for r in read_tsv(path):
            weekday = dt.date.fromisoformat(r["date"]).isoweekday()
            teacher = lambda n: ("INSERT INTO teachers(full_name) VALUES (?)", (n,))
            conn.execute(
                "INSERT INTO lessons(group_id, weekday, pair_no, week_type, subgroup, subject_id, kind,"
                " teacher_id, room_id, date, assistant_id) VALUES (?, ?, ?, 'all', ?, ?, ?, ?, ?, ?, ?)",
                (gid, weekday, r["pair"], r["subgroup"],
                 ref("subject", r["subject"], lambda n: ("INSERT INTO subjects(name) VALUES (?)", (n,))),
                 r["kind"], ref("teacher", r["teacher"], teacher),
                 ref("room", r["room"], lambda n: ("INSERT INTO rooms(name, building, kind, shared) VALUES (?, '', ?, ?)",
                                                   ROOMS.get(n, (n, "", 0)))),
                 r["date"], ref("teacher", r["assistant"], teacher)))
