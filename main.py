import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from passlib.hash import bcrypt
from pydantic import BaseModel

load_dotenv()

SETTINGS_PATH = Path("settings.json")
DEFAULT_SETTINGS = {
    "calories": 2000,
    "protein": 150,
    "carbs": 200,
    "fat": 65,
    "food_header": "## Food",
    "auth_enabled": False,
}

app = FastAPI(title="Calorie Tracker")
app.mount("/static", StaticFiles(directory="frontend"), name="static")


# ── Database ───────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id                          SERIAL PRIMARY KEY,
                    name                        TEXT NOT NULL UNIQUE,
                    password_hash               TEXT,
                    obsidian_vault_path         TEXT,
                    obsidian_daily_notes_folder TEXT NOT NULL DEFAULT 'Daily'
                )
            """)
            # Add columns to existing tables if upgrading
            for col, definition in [
                ("obsidian_vault_path",         "TEXT"),
                ("obsidian_daily_notes_folder", "TEXT NOT NULL DEFAULT 'Daily'"),
            ]:
                cur.execute(f"""
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}
                """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_log (
                    id         SERIAL PRIMARY KEY,
                    date       TEXT NOT NULL,
                    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    analysis   TEXT NOT NULL,
                    food_text  TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (date, user_id)
                )
            """)


# ── Settings ───────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
    return DEFAULT_SETTINGS.copy()


def save_settings(data: dict):
    SETTINGS_PATH.write_text(json.dumps(data, indent=2))


# ── Auth dependency ────────────────────────────────────────────────────────────

def get_current_user(
    x_user_id: Optional[str] = Header(None),
    x_session_token: Optional[str] = Header(None),
) -> int:
    settings = load_settings()
    if settings.get("auth_enabled"):
        if not x_session_token:
            raise HTTPException(401, "Authentication required")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM sessions WHERE token = %s", (x_session_token,)
                )
                row = cur.fetchone()
        if not row:
            raise HTTPException(401, "Invalid or expired session")
        return row["user_id"]
    else:
        if not x_user_id:
            raise HTTPException(400, "No user selected")
        try:
            return int(x_user_id)
        except ValueError:
            raise HTTPException(400, "Invalid user id")


# ── Data access ────────────────────────────────────────────────────────────────

def upsert_log(log_date: str, user_id: int, analysis: dict, food_text: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_log (date, user_id, analysis, food_text, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (date, user_id) DO UPDATE SET
                    analysis   = EXCLUDED.analysis,
                    food_text  = EXCLUDED.food_text,
                    updated_at = now()
            """, (log_date, user_id, json.dumps(analysis), food_text))


def get_history(user_id: int, limit: int = 30) -> list:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT date, analysis FROM daily_log WHERE user_id = %s ORDER BY date DESC LIMIT %s",
                (user_id, limit),
            )
            rows = cur.fetchall()
    return [
        {"date": row["date"], "totals": json.loads(row["analysis"]).get("totals", {})}
        for row in rows
    ]


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("frontend/index.html")


# Settings

@app.get("/api/settings")
def get_settings_route():
    return load_settings()


@app.put("/api/settings")
def put_settings(settings: dict):
    current = load_settings()
    current.update(settings)
    save_settings(current)
    return current


# Users

class UserCreate(BaseModel):
    name: str
    password: Optional[str] = None


class LoginRequest(BaseModel):
    user_id: int
    password: str


@app.get("/api/users")
def list_users():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM users ORDER BY name")
            return [dict(r) for r in cur.fetchall()]


@app.post("/api/users", status_code=201)
def create_user(body: UserCreate):
    pw_hash = bcrypt.hash(body.password) if body.password else None
    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO users (name, password_hash) VALUES (%s, %s) RETURNING id, name",
                    (body.name.strip(), pw_hash),
                )
                row = cur.fetchone()
            except psycopg2.errors.UniqueViolation:
                raise HTTPException(409, f"User '{body.name}' already exists")
    return dict(row)


@app.post("/api/auth/login")
def login(body: LoginRequest):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash FROM users WHERE id = %s", (body.user_id,)
            )
            user = cur.fetchone()
    if not user:
        raise HTTPException(404, "User not found")
    if not user["password_hash"] or not bcrypt.verify(body.password, user["password_hash"]):
        raise HTTPException(401, "Incorrect password")
    token = secrets.token_urlsafe(32)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (token, user_id) VALUES (%s, %s)",
                (token, body.user_id),
            )
    return {"token": token}


class UserUpdate(BaseModel):
    obsidian_vault_path: Optional[str] = None
    obsidian_daily_notes_folder: Optional[str] = None


@app.get("/api/users/me")
def get_me(user_id: int = Depends(get_current_user)):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, obsidian_vault_path, obsidian_daily_notes_folder FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    return dict(row)


@app.put("/api/users/me")
def update_me(body: UserUpdate, user_id: int = Depends(get_current_user)):
    updates = {}
    if body.obsidian_vault_path is not None:
        updates["obsidian_vault_path"] = body.obsidian_vault_path or None
    if body.obsidian_daily_notes_folder is not None:
        updates["obsidian_daily_notes_folder"] = body.obsidian_daily_notes_folder or "Daily"
    if updates:
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE users SET {set_clause} WHERE id = %s RETURNING id, name, obsidian_vault_path, obsidian_daily_notes_folder",
                    [*updates.values(), user_id],
                )
                return dict(cur.fetchone())
    return get_me(user_id)


@app.post("/api/auth/logout")
def logout(x_session_token: Optional[str] = Header(None)):
    if x_session_token:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE token = %s", (x_session_token,))
    return {"ok": True}


# ── Note parsing ───────────────────────────────────────────────────────────────

def read_food_section(vault_path: str, notes_folder: str, food_header: str) -> Optional[str]:
    today = date.today().strftime("%Y-%m-%d")
    note_path = Path(vault_path) / notes_folder / f"{today}.md"
    if not note_path.exists():
        return None

    text = note_path.read_text(encoding="utf-8")
    header_lower = food_header.strip().lower()

    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == header_lower:
            start = i + 1
            break

    if start is None:
        return None

    header_level = len(food_header) - len(food_header.lstrip("#"))
    section = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            depth = len(stripped) - len(stripped.lstrip("#"))
            if depth <= header_level:
                break
        section.append(line)

    content = "\n".join(section).strip()
    return content if content else None


# ── Write summary back to note ─────────────────────────────────────────────────

SUMMARY_START = "<!-- calorie-summary:start -->"
SUMMARY_END   = "<!-- calorie-summary:end -->"


def write_summary_to_note(vault_path: str, notes_folder: str, food_header: str, analysis: dict):
    today = date.today().strftime("%Y-%m-%d")
    note_path = Path(vault_path) / notes_folder / f"{today}.md"
    if not note_path.exists():
        return

    text = note_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    header_lower = food_header.strip().lower()

    section_start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == header_lower:
            section_start = i
            break
    if section_start is None:
        return

    header_level = len(food_header) - len(food_header.lstrip("#"))
    section_end = len(lines)
    for i in range(section_start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            depth = len(stripped) - len(stripped.lstrip("#"))
            if depth <= header_level:
                section_end = i
                break

    t = analysis["totals"]
    summary_lines = [
        SUMMARY_START,
        f"> **Calorie Summary** — {t['calories']} kcal | "
        f"Protein: {t['protein']}g | Carbs: {t['carbs']}g | Fat: {t['fat']}g",
        SUMMARY_END,
    ]

    food_section = lines[section_start:section_end]
    cleaned = []
    inside = False
    for line in food_section:
        if line.strip() == SUMMARY_START:
            inside = True
            continue
        if line.strip() == SUMMARY_END:
            inside = False
            continue
        if not inside:
            cleaned.append(line)

    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    updated_section = cleaned + [""] + summary_lines
    new_lines = lines[:section_start] + updated_section + lines[section_end:]
    note_path.write_text("\n".join(new_lines), encoding="utf-8")


# ── Claude analysis ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a nutrition expert. Given a free-form food log, estimate the calories and macros \
for each item. Be reasonable — use typical serving sizes when quantities are vague. \
Respond ONLY with valid JSON, no explanation, no markdown fences.

Return this structure:
{
  "meals": [
    {
      "name": "Breakfast",
      "items": [
        {"food": "Eggs x2", "calories": 140, "protein": 12, "carbs": 1, "fat": 10}
      ]
    }
  ],
  "totals": {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
}

Rules:
- Group items under the meal name they appear under (Breakfast, Lunch, Dinner, Snacks, etc.)
- If items have no meal header, group them under "Other"
- Calculate totals by summing all items
- Round all numbers to the nearest integer
- protein/carbs/fat are in grams
"""


def analyze_food(food_text: str) -> dict:
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Food log:\n\n{food_text}"}],
    )
    raw = message.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


# ── Data endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/analyze")
def analyze(user_id: int = Depends(get_current_user)):
    settings = load_settings()

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT obsidian_vault_path, obsidian_daily_notes_folder FROM users WHERE id = %s",
                (user_id,),
            )
            user = cur.fetchone()

    vault = (user["obsidian_vault_path"] or "").strip() if user else ""
    folder = (user["obsidian_daily_notes_folder"] or "Daily").strip() if user else "Daily"

    if not vault:
        raise HTTPException(400, "Obsidian vault path not set. Open Settings to configure it.")

    food_text = read_food_section(vault, folder, settings["food_header"])
    if food_text is None:
        today = date.today().strftime("%Y-%m-%d")
        raise HTTPException(
            404,
            f"No '{settings['food_header']}' section found in today's note "
            f"({today}.md). Add the header to your daily note to get started.",
        )

    try:
        result = analyze_food(food_text)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"Could not parse Claude response: {e}")
    except Exception as e:
        raise HTTPException(500, str(e))

    log_date = date.today().isoformat()
    upsert_log(log_date, user_id, result, food_text)
    write_summary_to_note(vault, folder, settings["food_header"], result)

    return {"analysis": result, "date": log_date, "food_text": food_text}


@app.get("/api/history")
def history(user_id: int = Depends(get_current_user)):
    return get_history(user_id)


init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
