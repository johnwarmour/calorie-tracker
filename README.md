# Calorie Tracker

Reads the `## Food` section from your Obsidian daily note, uses Claude to estimate calories and macros per item, and displays progress toward your daily targets.

## Setup

```bash
pip install anthropic fastapi "uvicorn[standard]" python-dotenv pydantic
```

Copy `.env.example` to `.env` and fill in your values:

```
ANTHROPIC_API_KEY=sk-ant-...
OBSIDIAN_VAULT_PATH=/Users/you/YourVault
OBSIDIAN_DAILY_NOTES_FOLDER=Daily
```

## Usage

```bash
python main.py
```

Open [http://localhost:8001](http://localhost:8001).

## Obsidian Daily Note Format

Add a `## Food` header (configurable in Settings) to your daily note. Group items under meal sub-headers — Claude will use them to organize the breakdown.

```markdown
## Food

### Breakfast
2 eggs scrambled
Coffee with oat milk

### Lunch
Chipotle burrito bowl with chicken

### Dinner
Salmon 6oz
Roasted broccoli
Brown rice 1 cup
```

Items with no meal sub-header are grouped under **Other**.

## Settings

Click the gear icon to configure daily targets for calories, protein, carbs, and fat, as well as the Obsidian food header name. Settings are saved locally in `settings.json`.
