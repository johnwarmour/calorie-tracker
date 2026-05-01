import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

SETTINGS_PATH = Path("settings.json")
DB_PATH = Path("tracker.db")
DEFAULT_SETTINGS = {
    "calories": 2000,
    "protein": 150,
    "carbs": 200,
    "fat": 65,
    "food_header": "## Food",
}

app = FastAPI(title="Calorie Tracker")
app.mount("/static", StaticFiles(directory="frontend"), name="static")


# ── Database ───────────────────────────────────────────────────────────────────

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_log (
                date        TEXT PRIMARY KEY,
                analysis    TEXT NOT NULL,
                food_text   TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def upsert_log(log_date: str, analysis: dict, food_text: str):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO daily_log (date, analysis, food_text, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(date) DO UPDATE SET
                analysis   = excluded.analysis,
                food_text  = excluded.food_text,
                updated_at = excluded.updated_at
        """, (log_date, json.dumps(analysis), food_text))

def get_history(limit: int = 30) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT date, analysis FROM daily_log ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        analysis = json.loads(row["analysis"])
        result.append({
            "date": row["date"],
            "totals": analysis.get("totals", {}),
        })
    return result


# ── Settings ───────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
    return DEFAULT_SETTINGS.copy()


def save_settings(data: dict):
    SETTINGS_PATH.write_text(json.dumps(data, indent=2))


@app.get("/")
def root():
    return FileResponse("frontend/index.html")


@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.put("/api/settings")
def put_settings(settings: dict):
    current = load_settings()
    current.update(settings)
    save_settings(current)
    return current


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

    # Find the food section start
    section_start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == header_lower:
            section_start = i
            break
    if section_start is None:
        return

    # Find where the food section ends (next same-or-higher header)
    header_level = len(food_header) - len(food_header.lstrip("#"))
    section_end = len(lines)
    for i in range(section_start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            depth = len(stripped) - len(stripped.lstrip("#"))
            if depth <= header_level:
                section_end = i
                break

    # Build summary block
    t = analysis["totals"]
    summary_lines = [
        SUMMARY_START,
        f"> **Calorie Summary** — {t['calories']} kcal | "
        f"Protein: {t['protein']}g | Carbs: {t['carbs']}g | Fat: {t['fat']}g",
        SUMMARY_END,
    ]

    # Strip any existing summary from within the section
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

    # Drop trailing blank lines before appending summary
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


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/api/analyze")
def analyze():
    settings = load_settings()
    vault = os.getenv("OBSIDIAN_VAULT_PATH", "")
    folder = os.getenv("OBSIDIAN_DAILY_NOTES_FOLDER", "Daily")

    if not vault:
        raise HTTPException(400, "OBSIDIAN_VAULT_PATH not set in .env")

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
    upsert_log(log_date, result, food_text)
    write_summary_to_note(vault, folder, settings["food_header"], result)

    return {"analysis": result, "date": log_date, "food_text": food_text}


@app.get("/api/history")
def history():
    return get_history()


init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
