from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Optional
from datetime import datetime, date, timezone, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo
import base64
import hashlib
import hmac
import json
import sqlite3
import os
import uuid as uuid_lib
import urllib.request as url_req
from urllib.parse import parse_qsl

app = FastAPI(title="Habits Tracker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.environ.get("DB_PATH", "/data/habits.db")
BOT_DB_PATH = "/data/bot.db"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
LOGS_DIR = os.environ.get("LOGS_DIR", "/logs")
ENABLE_LOGGING = os.environ.get("ENABLE_LOGGING", "true").lower() == "true"
_TZ = ZoneInfo(os.environ.get("TZ", "UTC"))

YOOKASSA_SHOP_ID  = os.environ.get("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET   = os.environ.get("YOOKASSA_SECRET", "")
SUBSCRIPTION_PRICE = os.environ.get("SUBSCRIPTION_PRICE", "199.00")
TRIAL_SECONDS = int(os.environ.get("TRIAL_SECONDS", "604800"))  # 7 days; set 600 for testing
_ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()}
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
if not BOT_USERNAME and BOT_TOKEN:
    try:
        _me_req = url_req.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", method="GET")
        with url_req.urlopen(_me_req, timeout=5) as _r:
            BOT_USERNAME = json.loads(_r.read()).get("result", {}).get("username", "")
    except Exception:
        pass


def _get_group_title(chat_id: int) -> str | None:
    try:
        conn = sqlite3.connect(BOT_DB_PATH)
        row = conn.execute("SELECT title FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception:
        pass
    # Fallback: ask Telegram API directly and cache result
    if BOT_TOKEN:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?chat_id={chat_id}"
            with url_req.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read())
            title = data.get("result", {}).get("title")
            if title:
                try:
                    conn = sqlite3.connect(BOT_DB_PATH)
                    conn.execute("CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
                    conn.execute("INSERT OR REPLACE INTO groups (chat_id, title) VALUES (?, ?)", (chat_id, title))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
                return title
        except Exception:
            pass
    return None


def _log_path(chat_id: int) -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    prefix = "group" if chat_id < 0 else "direct"
    return os.path.join(LOGS_DIR, f"{prefix}_{abs(chat_id)}.log")


def log(chat_id: int, user_label: str, action: str):
    if not ENABLE_LOGGING:
        return
    if chat_id < 0:
        title = _get_group_title(chat_id) or str(chat_id)
        action = f"{action} in group '{title}'"
    ts = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    with open(_log_path(chat_id), "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {user_label}: {action}\n")


def validate_init_data(init_data: str) -> dict | None:
    """Validate Telegram initData HMAC. Returns parsed fields or None if invalid."""
    if not init_data or not BOT_TOKEN:
        return None
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    hash_ = parsed.pop("hash", None)
    if not hash_:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(hash_, expected):
        return None
    return parsed


def extract_chat_id(parsed: dict) -> int | None:
    """Use group chat id from initData, fall back to user id for private chats."""
    chat_str = parsed.get("chat")
    if chat_str:
        try:
            return json.loads(chat_str)["id"]
        except (json.JSONDecodeError, KeyError):
            pass
    user_str = parsed.get("user")
    if user_str:
        try:
            return json.loads(user_str)["id"]
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def extract_user_id(parsed: dict) -> int | None:
    user_str = parsed.get("user")
    if user_str:
        try:
            return int(json.loads(user_str)["id"])
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    return None


def extract_user_label(parsed: dict) -> str:
    user_str = parsed.get("user", "")
    if user_str:
        try:
            u = json.loads(user_str)
            name = u.get("first_name", "")
            if u.get("last_name"):
                name += f" {u['last_name']}"
            uid = u.get("id", "?")
            if u.get("username"):
                return f"{name} (@{u['username']}, id={uid})"
            return f"{name} (id={uid})"
        except (json.JSONDecodeError, KeyError):
            pass
    return "unknown"


class TelegramAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # YooKassa webhook has its own auth (payment verification)
        if request.url.path == "/api/yookassa/webhook":
            return await call_next(request)

        init_data = request.headers.get("X-Telegram-Init-Data", "") or request.query_params.get("init_data", "")
        parsed = validate_init_data(init_data)
        if parsed is None:
            return Response(status_code=401, content="Unauthorized")

        override = request.headers.get("X-Chat-Id") or request.query_params.get("chat_id")
        if override:
            try:
                chat_id = int(override)
            except ValueError:
                return Response(status_code=400, content="Invalid chat id")
        else:
            chat_id = extract_chat_id(parsed)

        if chat_id is None:
            return Response(status_code=401, content="Unauthorized")

        request.state.chat_id = chat_id
        request.state.user_id = extract_user_id(parsed) or chat_id
        request.state.user_label = extract_user_label(parsed)
        return await call_next(request)


app.add_middleware(TelegramAuthMiddleware)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS persons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(chat_id, name)
        );

        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(person_id, title),
            FOREIGN KEY(person_id) REFERENCES persons(id)
        );

        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            habit_id INTEGER NOT NULL,
            check_date TEXT NOT NULL,
            status TEXT CHECK(status IN ('yes','no')) NOT NULL,
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(person_id, habit_id, check_date),
            FOREIGN KEY(person_id) REFERENCES persons(id),
            FOREIGN KEY(habit_id) REFERENCES habits(id)
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id  INTEGER NOT NULL,
            chat_id  INTEGER NOT NULL,
            trial_start TEXT NOT NULL DEFAULT (datetime('now')),
            paid_until  TEXT,
            PRIMARY KEY (user_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            chat_id    INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)

    conn.commit()
    conn.close()


init_db()


# ── Models ──────────────────────────────────────────

class PersonCreate(BaseModel):
    name: str

class HabitCreate(BaseModel):
    person_id: int
    title: str

class CheckUpsert(BaseModel):
    person_id: int
    habit_id: int
    check_date: str
    status: Optional[str] = None


# ── Persons ─────────────────────────────────────────

@app.get("/api/persons")
def get_persons(request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    rows = conn.execute("SELECT * FROM persons WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/persons")
def create_person(body: PersonCreate, request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    try:
        conn.execute("INSERT INTO persons (chat_id, name) VALUES (?, ?)", (chat_id, body.name.strip()))
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Такой участник уже есть")
    conn.close()
    log(chat_id, request.state.user_label, f"added person \"{body.name.strip()}\"")
    return {"ok": True}

@app.delete("/api/persons/{person_id}")
def delete_person(person_id: int, request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    row = conn.execute("SELECT name FROM persons WHERE id=? AND chat_id=?", (person_id, chat_id)).fetchone()
    person_name = row["name"] if row else f"id={person_id}"
    conn.execute("DELETE FROM checks WHERE person_id=?", (person_id,))
    conn.execute("DELETE FROM habits WHERE person_id=?", (person_id,))
    conn.execute("DELETE FROM persons WHERE id=? AND chat_id=?", (person_id, chat_id))
    conn.commit()
    conn.close()
    log(chat_id, request.state.user_label, f"deleted person \"{person_name}\"")
    return {"ok": True}


# ── Habits ───────────────────────────────────────────

@app.get("/api/habits")
def get_habits(request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    rows = conn.execute("""
        SELECT h.* FROM habits h
        JOIN persons p ON h.person_id = p.id
        WHERE p.chat_id = ?
        ORDER BY h.person_id, h.id
    """, (chat_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/habits")
def create_habit(body: HabitCreate, request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    person = conn.execute(
        "SELECT id FROM persons WHERE id=? AND chat_id=?", (body.person_id, chat_id)
    ).fetchone()
    if not person:
        raise HTTPException(403, "Forbidden")
    try:
        conn.execute(
            "INSERT INTO habits (person_id, title) VALUES (?, ?)",
            (body.person_id, body.title.strip())
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Такая привычка у этого участника уже есть")
    person_row = conn.execute("SELECT name FROM persons WHERE id=?", (body.person_id,)).fetchone()
    person_name = person_row["name"] if person_row else f"id={body.person_id}"
    conn.close()
    log(chat_id, request.state.user_label, f"added habit \"{body.title.strip()}\" for person \"{person_name}\"")
    return {"ok": True}

@app.delete("/api/habits/{habit_id}")
def delete_habit(habit_id: int, request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    row = conn.execute("SELECT title FROM habits WHERE id=?", (habit_id,)).fetchone()
    habit_title = row["title"] if row else f"id={habit_id}"
    conn.execute("""
        DELETE FROM checks WHERE habit_id=? AND person_id IN (
            SELECT id FROM persons WHERE chat_id=?
        )
    """, (habit_id, chat_id))
    conn.execute("""
        DELETE FROM habits WHERE id=? AND person_id IN (
            SELECT id FROM persons WHERE chat_id=?
        )
    """, (habit_id, chat_id))
    conn.commit()
    conn.close()
    log(chat_id, request.state.user_label, f"deleted habit \"{habit_title}\"")
    return {"ok": True}


# ── Checks ───────────────────────────────────────────

@app.get("/api/checks")
def get_checks(year: int, month: int, request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    prefix = f"{year:04d}-{month:02d}"
    rows = conn.execute("""
        SELECT c.* FROM checks c
        JOIN persons p ON c.person_id = p.id
        WHERE p.chat_id = ? AND c.check_date LIKE ?
    """, (chat_id, f"{prefix}%")).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/checks")
def upsert_check(body: CheckUpsert, request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    person = conn.execute(
        "SELECT id FROM persons WHERE id=? AND chat_id=?", (body.person_id, chat_id)
    ).fetchone()
    if not person:
        raise HTTPException(403, "Forbidden")
    habit_row = conn.execute("SELECT title FROM habits WHERE id=?", (body.habit_id,)).fetchone()
    habit_title = habit_row["title"] if habit_row else f"id={body.habit_id}"
    if body.status is None:
        conn.execute(
            "DELETE FROM checks WHERE person_id=? AND habit_id=? AND check_date=?",
            (body.person_id, body.habit_id, body.check_date)
        )
        action = f"cleared \"{habit_title}\" on {body.check_date}"
    else:
        conn.execute("""
            INSERT INTO checks (person_id, habit_id, check_date, status, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(person_id, habit_id, check_date)
            DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
        """, (body.person_id, body.habit_id, body.check_date, body.status))
        mark = "✅" if body.status == "yes" else "❌"
        action = f"marked \"{habit_title}\" on {body.check_date} as {mark}"
    conn.commit()
    conn.close()
    log(chat_id, request.state.user_label, action)
    return {"ok": True}


# ── Stats ────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(year: int, month: int, request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    prefix = f"{year:04d}-{month:02d}"
    persons = [dict(r) for r in conn.execute(
        "SELECT * FROM persons WHERE chat_id=? ORDER BY id", (chat_id,)
    ).fetchall()]
    checks = conn.execute("""
        SELECT c.* FROM checks c
        JOIN persons p ON c.person_id = p.id
        WHERE p.chat_id = ? AND c.check_date LIKE ?
    """, (chat_id, f"{prefix}%")).fetchall()
    conn.close()

    result = {}
    for p in persons:
        yes = sum(1 for c in checks if c["person_id"] == p["id"] and c["status"] == "yes")
        no  = sum(1 for c in checks if c["person_id"] == p["id"] and c["status"] == "no")
        total = yes + no
        result[p["id"]] = {
            "name": p["name"],
            "yes": yes,
            "no": no,
            "pct": round(yes / total * 100) if total > 0 else None
        }
    return result


# ── Streaks ──────────────────────────────────────────

@app.get("/api/streaks")
def get_streaks(request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    rows = conn.execute("""
        SELECT c.person_id, c.habit_id, c.check_date, c.status
        FROM checks c
        JOIN persons p ON c.person_id = p.id
        WHERE p.chat_id = ?
        ORDER BY c.person_id, c.habit_id, c.check_date
    """, (chat_id,)).fetchall()
    conn.close()

    groups = defaultdict(list)
    for r in rows:
        groups[(r["person_id"], r["habit_id"])].append((r["check_date"], r["status"]))

    today = date.today()
    result = []
    for (pid, hid), entries in groups.items():
        entries.sort(key=lambda x: x[0])
        best = 0
        cur = 0
        prev_date = None
        for date_str, status in entries:
            if status != "yes":
                prev_date = None
                cur = 0
                continue
            d = date.fromisoformat(date_str)
            if prev_date is not None and (d - prev_date).days == 1:
                cur += 1
            else:
                cur = 1
            best = max(best, cur)
            prev_date = d
        if prev_date and (today - prev_date).days > 1:
            cur = 0
        result.append({"person_id": pid, "habit_id": hid, "current": cur, "best": best})
    return result


# ── Export / Import ──────────────────────────────────

@app.get("/api/export")
def export_db(request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    persons = [dict(r) for r in conn.execute(
        "SELECT id, name FROM persons WHERE chat_id=? ORDER BY id", (chat_id,)
    ).fetchall()]
    habits = [dict(r) for r in conn.execute("""
        SELECT h.id, h.person_id, h.title FROM habits h
        JOIN persons p ON h.person_id = p.id
        WHERE p.chat_id = ? ORDER BY h.id
    """, (chat_id,)).fetchall()]
    checks = [dict(r) for r in conn.execute("""
        SELECT c.person_id, c.habit_id, c.check_date, c.status FROM checks c
        JOIN persons p ON c.person_id = p.id
        WHERE p.chat_id = ?
    """, (chat_id,)).fetchall()]
    conn.close()
    log(chat_id, request.state.user_label, "exported data")
    content = json.dumps({"persons": persons, "habits": habits, "checks": checks}, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=habits_backup.json"}
    )


@app.post("/api/import")
async def import_db(request: Request, file: UploadFile = File(...)):
    chat_id = request.state.chat_id
    contents = await file.read()
    try:
        data = json.loads(contents)
        persons_src = data["persons"]
        habits_src  = data["habits"]
        checks_src  = data["checks"]
    except Exception:
        raise HTTPException(400, "Invalid backup file")

    conn = get_db()
    try:
        conn.execute("DELETE FROM checks WHERE person_id IN (SELECT id FROM persons WHERE chat_id=?)", (chat_id,))
        conn.execute("DELETE FROM habits WHERE person_id IN (SELECT id FROM persons WHERE chat_id=?)", (chat_id,))
        conn.execute("DELETE FROM persons WHERE chat_id=?", (chat_id,))

        person_id_map = {}
        for p in persons_src:
            cur = conn.execute("INSERT INTO persons (chat_id, name) VALUES (?, ?)", (chat_id, p["name"]))
            person_id_map[p["id"]] = cur.lastrowid

        habit_id_map = {}
        for h in habits_src:
            new_pid = person_id_map.get(h["person_id"])
            if new_pid is None:
                continue
            cur = conn.execute("INSERT INTO habits (person_id, title) VALUES (?, ?)", (new_pid, h["title"]))
            habit_id_map[h["id"]] = cur.lastrowid

        for c in checks_src:
            new_pid = person_id_map.get(c["person_id"])
            new_hid = habit_id_map.get(c["habit_id"])
            if new_pid is None or new_hid is None:
                continue
            conn.execute("""
                INSERT OR REPLACE INTO checks (person_id, habit_id, check_date, status, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (new_pid, new_hid, c["check_date"], c["status"]))

        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(400, f"Import failed: {e}")
    conn.close()
    log(chat_id, request.state.user_label, "imported data")
    return {"ok": True}


# ── Tamagotchi ────────────────────────────────────────

# Points needed per level (index 0 = L1→L2, ..., index 44 = L45 cap)
_LEVEL_PTS = (
    [4] * 5 +   # L1-L5
    [6] * 5 +   # L6-L10
    [10] * 5 +  # L11-L15
    [14] * 5 +  # L16-L20
    [18] * 5 +  # L21-L25
    [22] * 5 +  # L26-L30
    [26] * 5 +  # L31-L35
    [30] * 5 +  # L36-L40
    [30] * 5    # L41-L45
)

# Cumulative points at the START of each level (index = level-1)
_LEVEL_THRESHOLDS = [0]
for _p in _LEVEL_PTS[:-1]:
    _LEVEL_THRESHOLDS.append(_LEVEL_THRESHOLDS[-1] + _p)

# Evolution unlocks at these cumulative point totals (8 evolutions)
_EVO_THRESHOLDS = [0, 20, 50, 100, 170, 370, 650, 800]


def _compute_tama(total_pts: int) -> dict:
    """Return level (1-45), evo (1-8), pts_in_level, pts_for_level, next_evo_level."""
    total_pts = min(total_pts, 800)
    # Level
    level = 1
    for i, t in enumerate(_LEVEL_THRESHOLDS):
        if total_pts >= t:
            level = i + 1
        else:
            break
    level = min(level, 45)
    # Evo
    evo = 1
    for i, t in enumerate(_EVO_THRESHOLDS):
        if total_pts >= t:
            evo = i + 1
    # Next evo level
    next_evo_level = None
    if evo < 8:
        next_evo_pts = _EVO_THRESHOLDS[evo]  # index = current evo (0-based next)
        for i, t in enumerate(_LEVEL_THRESHOLDS):
            if t >= next_evo_pts:
                next_evo_level = i + 1
                break
        if next_evo_level is None:
            next_evo_level = 45
    # Progress within current level
    lvl_start = _LEVEL_THRESHOLDS[level - 1]
    lvl_pts = _LEVEL_PTS[level - 1]
    pts_in_level = total_pts - lvl_start
    return {
        "level": level,
        "evo": evo,
        "pts_in_level": pts_in_level,
        "pts_for_level": lvl_pts,
        "total_pts": total_pts,
        "next_evo_level": next_evo_level,
    }


@app.get("/api/tamagotchi")
def get_tamagotchi(request: Request):
    chat_id = request.state.chat_id
    conn = get_db()
    persons = [dict(r) for r in conn.execute(
        "SELECT id, name FROM persons WHERE chat_id=? ORDER BY id", (chat_id,)
    ).fetchall()]
    result = []
    for p in persons:
        yes_count = conn.execute(
            "SELECT COUNT(*) FROM checks WHERE person_id=? AND status='yes'", (p["id"],)
        ).fetchone()[0]
        tama = _compute_tama(yes_count)
        tama["person_id"] = p["id"]
        tama["name"] = p["name"]
        result.append(tama)
    conn.close()
    return result


# ── Subscriptions ─────────────────────────────────────

def _get_or_create_sub(user_id: int, chat_id: int) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO subscriptions (user_id, chat_id, trial_start) VALUES (?, ?, datetime('now'))",
            (user_id, chat_id)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        ).fetchone()
    conn.close()
    return dict(row)


def _sub_status(sub: dict) -> dict:
    now = datetime.now(timezone.utc)

    # Check paid subscription first
    if sub.get("paid_until"):
        paid_until = datetime.fromisoformat(sub["paid_until"]).replace(tzinfo=timezone.utc)
        if now < paid_until:
            return {
                "active": True,
                "reason": "paid",
                "trial_seconds_left": None,
                "paid_until": paid_until.isoformat(),
            }

    # Check trial
    trial_start = datetime.fromisoformat(sub["trial_start"]).replace(tzinfo=timezone.utc)
    trial_end = trial_start + timedelta(seconds=TRIAL_SECONDS)
    if now < trial_end:
        seconds_left = int((trial_end - now).total_seconds())
        return {
            "active": True,
            "reason": "trial",
            "trial_seconds_left": seconds_left,
            "paid_until": None,
        }

    return {"active": False, "reason": "expired", "trial_seconds_left": 0, "paid_until": None}


@app.get("/api/subscription")
def get_subscription(request: Request):
    user_id = request.state.user_id
    sub = _get_or_create_sub(user_id, request.state.chat_id)
    result = _sub_status(sub)
    result["is_admin"] = user_id in _ADMIN_IDS
    return result


@app.post("/api/create-payment")
def create_payment(request: Request):
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET:
        raise HTTPException(503, "Payment system not configured")

    user_id = request.state.user_id
    chat_id = request.state.chat_id
    auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET}".encode()).decode()
    payload = json.dumps({
        "amount": {"value": SUBSCRIPTION_PRICE, "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": f"https://t.me/{BOT_USERNAME}?startapp=g{abs(chat_id)}" if BOT_USERNAME else WEBAPP_URL},
        "description": "Подписка Habits Tracker на 30 дней",
        "metadata": {"user_id": str(user_id), "chat_id": str(chat_id)},
        "capture": True,
    }).encode()

    req = url_req.Request(
        "https://api.yookassa.ru/v3/payments",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
            "Idempotence-Key": str(uuid_lib.uuid4()),
        },
        method="POST",
    )
    try:
        with url_req.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        raise HTTPException(502, f"Payment creation failed: {e}")

    payment_id  = data["id"]
    payment_url = data["confirmation"]["confirmation_url"]

    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO payments (payment_id, user_id, chat_id, status) VALUES (?, ?, ?, 'pending')",
        (payment_id, user_id, chat_id)
    )
    conn.commit()
    conn.close()

    return {"payment_url": payment_url}


@app.post("/api/yookassa/webhook")
async def yookassa_webhook(request: Request):
    body = await request.body()
    try:
        event = json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if event.get("event") != "payment.succeeded":
        return {"ok": True}

    payment_obj = event.get("object", {})
    payment_id  = payment_obj.get("id")
    metadata    = payment_obj.get("metadata", {})

    try:
        user_id = int(metadata["user_id"])
        chat_id = int(metadata["chat_id"])
    except (KeyError, ValueError):
        raise HTTPException(400, "Missing metadata")

    # Verify payment with YooKassa (don't trust webhook body alone)
    if YOOKASSA_SHOP_ID and YOOKASSA_SECRET:
        auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET}".encode()).decode()
        verify_req = url_req.Request(
            f"https://api.yookassa.ru/v3/payments/{payment_id}",
            headers={"Authorization": f"Basic {auth}"},
            method="GET",
        )
        try:
            with url_req.urlopen(verify_req, timeout=10) as resp:
                payment_data = json.loads(resp.read())
        except Exception:
            raise HTTPException(502, "Could not verify payment")
        if payment_data.get("status") != "succeeded":
            return {"ok": True}

    # Extend subscription by 30 days
    conn = get_db()
    sub = conn.execute(
        "SELECT paid_until FROM subscriptions WHERE user_id=? AND chat_id=?",
        (user_id, chat_id)
    ).fetchone()

    now = datetime.now(timezone.utc)
    base = now
    if sub and sub["paid_until"]:
        existing = datetime.fromisoformat(sub["paid_until"]).replace(tzinfo=timezone.utc)
        if existing > now:
            base = existing
    new_paid_until = (base + timedelta(days=30)).isoformat()

    conn.execute("""
        INSERT INTO subscriptions (user_id, chat_id, trial_start, paid_until)
        VALUES (?, ?, datetime('now'), ?)
        ON CONFLICT(user_id, chat_id) DO UPDATE SET paid_until=excluded.paid_until
    """, (user_id, chat_id, new_paid_until))
    conn.execute("UPDATE payments SET status='succeeded' WHERE payment_id=?", (payment_id,))
    conn.commit()
    conn.close()

    # Notify group chat
    if BOT_TOKEN and chat_id < 0:
        paid_date = new_paid_until[:10]  # YYYY-MM-DD
        # Get user display name
        user_name = f"id{user_id}"
        try:
            info_req = url_req.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?chat_id={user_id}",
                method="GET",
            )
            with url_req.urlopen(info_req, timeout=5) as r:
                info = json.loads(r.read()).get("result", {})
            first = info.get("first_name", "")
            last  = info.get("last_name", "")
            uname = info.get("username", "")
            user_name = (first + (" " + last if last else "")).strip()
            if uname:
                user_name = f'<a href="tg://user?id={user_id}">{user_name}</a>'
            else:
                user_name = f'<a href="tg://user?id={user_id}">{user_name or f"id{user_id}"}</a>'
        except Exception:
            pass
        notify_payload = json.dumps({
            "chat_id": chat_id,
            "text": f"✅ {user_name} оформил подписку до <b>{paid_date}</b>",
            "parse_mode": "HTML",
        }).encode()
        notify_req = url_req.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=notify_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            url_req.urlopen(notify_req, timeout=5)
        except Exception:
            pass

    return {"ok": True}
