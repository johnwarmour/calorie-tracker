import json
import os
import re
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
DEFAULT_SETTINGS = {
    "calories": 2000,
    "protein": 150,
    "carbs": 200,
    "fat": 65,
    "food_header": "## Food",
}

app = FastAPI(title="Calorie Tracker")
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
def root():
    return FileResponse("frontend/index.html")


# ── Settings ───────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
    return DEFAULT_SETTINGS.copy()


def save_settings(data: dict):
    SETTINGS_PATH.write_text(json.dumps(data, indent=2))


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

    # Collect lines until next same-level or higher header
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


# ── Analyze endpoint ───────────────────────────────────────────────────────────

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

    return {"analysis": result, "date": date.today().isoformat(), "food_text": food_text}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
