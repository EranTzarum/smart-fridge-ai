# Smart-Fridge AI

An AI-native smart fridge proof-of-concept that combines a receipt-scanning ingestion engine with a conversational personal chef — powered by **Google Gemini 2.5 Flash** and **Supabase** (PostgreSQL REST API).

The system is built around a strict, enforced boundary: the LLM handles perception and creativity; Python handles every deterministic decision.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [The Boundary: Deterministic Python vs. Generative LLM](#the-boundary-deterministic-python-vs-generative-llm)
3. [scanner.py — Receipt Ingestion Engine](#scannerpy--receipt-ingestion-engine)
   - [Layer Map](#scanner-layer-map)
   - [OCR Deduplication Logic](#ocr-deduplication-logic)
   - [Smart Upsert Workflow](#smart-upsert-workflow)
4. [chef_agent.py — Personal Chef Engine](#chef_agentpy--personal-chef-engine)
   - [Layer Map](#chef-layer-map)
   - [Stateful Chat Architecture](#stateful-chat-architecture)
   - [Intent Classification Engine](#intent-classification-engine)
   - [Diner Scaling Flow](#diner-scaling-flow)
   - [Smart Shopping List Flow](#smart-shopping-list-flow)
5. [End-to-End Data Flow](#end-to-end-data-flow)
6. [Prerequisites & Setup](#prerequisites--setup)
7. [Running the CLI Agents](#running-the-cli-agents)
8. [Database Schema](#database-schema)

---

## System Overview

| Agent | Script | Entry Point | Purpose |
|---|---|---|---|
| **Scanner** | `scanner.py` | `run_scanner(image_path)` | Parses a grocery receipt image and upserts items into the virtual fridge inventory |
| **Chef** | `chef_agent.py` | `run_chef_agent()` | Reads the full active inventory and conducts a stateful Hebrew conversation to suggest, refine, scale, and log cooked recipes |

Both scripts are **Cloud Function–ready**: their entry points accept no runtime dependencies beyond environment variables and can be invoked from any orchestrator.

---

## The Boundary: Deterministic Python vs. Generative LLM

The central design principle of this system is that **the LLM is never trusted with deterministic decisions**. Every piece of logic that has a single correct answer lives in Python.

### What the LLM owns

| Responsibility | Detail |
|---|---|
| **Vision / OCR** | Reading raw receipt images and extracting item names |
| **Name normalisation** | Stripping brand names, weights, and percentages → clean generic Hebrew (`"חלב טרה 3% 1 ליטר"` → `"חלב"`) |
| **Categorisation** | Classifying each item into one of seven predefined Hebrew categories |
| **Expiry estimation** | Estimating storage duration in days by food type and typical handling (e.g., frozen meat vs. fresh dairy) |
| **Culinary creativity** | Designing recipes that match the user's stated vibe, incorporating available fridge items |
| **Semantic ingredient matching** | Resolving vague requests like `"בשר"` to available items by category, not string comparison |
| **Recipe revision** | Adapting a prior recipe to user feedback across multiple turns |
| **Diner scaling** | Recalculating all `quantity_used` values for a given number of diners |

### What Python owns

| Responsibility | Detail |
|---|---|
| **All date logic** | `purchase_date = datetime.now()`. `expiry_date = purchase_date + timedelta(days=N)`. The LLM never sees or produces a date. |
| **Adaptive fuzzy deduplication** | 80 % standard threshold; drops to 55 % within 15 minutes of a prior scan to collapse OCR-noise variants |
| **Hebrew plural normalisation** | Strips `ים`, `ות`, `יות` suffixes before fuzzy comparison so `"תפוחים"` matches an existing `"תפוח"` DB row |
| **Non-food filtering** | Deposits, bags, and packaging are removed before any data reaches the LLM |
| **Intent classification** | User responses are classified into `confirm / revise / cancel` by a keyword rule engine — no LLM call |
| **JSON extraction** | A brace-depth parser extracts valid JSON from LLM output regardless of markdown wrapping or trailing prose |
| **Quantity arithmetic** | Float deduction with `round(..., 3)` to prevent floating-point noise; correct handling of fractional quantities (e.g., 0.25 kg) |
| **DB persistence** | All Supabase reads, smart upserts, PATCH operations, and shopping list inserts |
| **Display / rendering** | All CLI formatting and ANSI colour codes |
| **Loop control** | Revision counter hard-capped at `MAX_REVISIONS = 5` |

---

## scanner.py — Receipt Ingestion Engine

### Scanner Layer Map

```
receipt image (JPG)
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 1 — LLM  (Gemini Vision)                           │
│  analyze_receipt()                                        │
│  → items[]: item_name, category, quantity,                │
│             estimated_expiry_days                         │
│  No dates. Ever.                                          │
└───────────────────────┬───────────────────────────────────┘
                        │ raw LLM payload
                        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 2 — DB Helpers  (Deterministic / I/O)              │
│  get_latest_item_timestamp()  ← adaptive threshold probe  │
│  get_active_items()           ← full inventory for dedup  │
│  update_consumed_items()      ← batch soft-delete         │
└───────────────────────┬───────────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 3 — Business Logic  (Deterministic / Pure)         │
│  normalize_hebrew_for_matching()  ← plural suffix strip   │
│  detect_scan_mode()               ← 0.80 or 0.55         │
│  find_best_match()                ← difflib fuzzy match   │
│  build_fridge_rows()              ← attach Python dates   │
└───────────────────────┬───────────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 4 — Orchestration  (Entry Point)                   │
│  save_to_db() → run_scanner()                             │
│  Smart upsert: skip duplicate | retire old | insert new   │
└───────────────────────┬───────────────────────────────────┘
                        │
                        ▼
                  Supabase fridge_items
```

### OCR Deduplication Logic

Scanning the same receipt twice within minutes is a real operational hazard: Gemini may return `"מלפפון"` on the first scan and `"מלפפון "` (trailing space) on the second. At a standard 80 % threshold, `difflib` treats these as different items and inserts a duplicate.

The deduplication pipeline has three interlocking components:

#### 1. Hebrew Plural Normalisation (`normalize_hebrew_for_matching`)

Applied to **both** the incoming item name and every existing DB key before any comparison. This is a comparison-only transform — the normalised form is never written to the database.

| Input | Suffix stripped | Normalised |
|---|---|---|
| `"תפוחים"` | `"ים"` | `"תפוח"` |
| `"עגבניות"` | `"יות"` | `"עגבני"` |
| `"ביצים"` | `"ים"` | `"ביצ"` |

Suffixes are tried longest-first (`"יות"` before `"ות"`) to prevent partial stripping.

#### 2. Adaptive Threshold (`detect_scan_mode`)

Before comparing anything, the scanner probes the DB for the `created_at` timestamp of the most recently inserted active item.

| Condition | Threshold | Behaviour |
|---|---|---|
| Last insert > 15 minutes ago | **0.80** | Standard — avoids false-positive merges across genuinely different shopping trips |
| Last insert ≤ 15 minutes ago | **0.55** | Aggressive — collapses OCR-noise variants that differ by whitespace, punctuation, or minor character drift |

If the timestamp query fails for any reason, the function fails silently and returns the safe 0.80 default.

#### 3. Fuzzy Match with Normalised Key Mapping (`find_best_match`)

```
incoming name  →  normalize_hebrew_for_matching()  →  normalised_target
                                                              │
DB keys        →  normalize_hebrew_for_matching()  →  {normalised: original}
                                                              │
                              difflib.get_close_matches()  ──┘
                                                              │
                              map winner back to original DB row
                              return full DB dict (with id, purchase_date)
```

The double-normalisation ensures that a new receipt item `"תפוחים"` correctly matches the existing DB entry `"תפוח"`, and the original (un-normalised) name is used for all subsequent DB lookups.

### Smart Upsert Workflow

`save_to_db()` runs five ordered steps after receiving the LLM payload:

```
1. Probe DB for most recent insert timestamp
        │
        └→ detect_scan_mode() → threshold (0.55 or 0.80)

2. Fetch full active inventory
        │
        └→ {item_name: {id, purchase_date, ...}} dict

3. Set purchase_date = datetime.now()   ← Python. Always. Not the LLM.

4. For each candidate row from build_fridge_rows():
        │
        ├─ find_best_match() returns a hit?
        │       │
        │       ├─ Condition A: hit.purchase_date == today
        │       │       └→ SKIP (same-day duplicate scan)
        │       │
        │       └─ Condition B: hit.purchase_date < today
        │               └→ queue old row for soft-delete (restock)
        │
        └─ No hit → queue for INSERT (genuinely new item)

5. Batch PATCH old rows → status='consumed'
   Batch POST new rows → status='active'
```

---

## chef_agent.py — Personal Chef Engine

### Chef Layer Map

```
Supabase fridge_items (all active)
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 1 — Data Retrieval  (Deterministic)                │
│  get_all_active_items()                                   │
│  → fetches entire active inventory (no expiry filter)     │
│  → _is_food_item() strips deposits/bags/packaging         │
│  → sorted by expiry_date ascending                        │
└───────────────────────┬───────────────────────────────────┘
                        │ food_items[]
                        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 2 — LLM Chat  (Probabilistic / Stateful)           │
│  _create_chef_chat()        ← SYSTEM_INSTRUCTION loaded   │
│  _build_initial_prompt()    ← items + user vibe           │
│  _send_and_parse()          ← chat turn + JSON extract    │
│  _build_revision_prompt()   ← feedback only (no re-send)  │
└───────────────────────┬───────────────────────────────────┘
                        │ recipe dict
                        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 3 — Recipe Display  (Deterministic)                │
│  _format_recipe_for_display()                             │
│  → chef_message (bold yellow ANSI)                        │
│  → recipe_name + tagline                                  │
│  → excluded_items (culinary notes)                        │
│  → used_fridge_items + pantry_staples_needed              │
│  → numbered instructions                                  │
└───────────────────────┬───────────────────────────────────┘
                        │ user input
                        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 4 — DB Consumption  (Deterministic)                │
│  consume_recipe_items()   ← float deduction + rounding    │
│  _patch_fridge_item()     ← PATCH quantity or status      │
│  add_to_smart_list()      ← POST to smart_shopping_list  │
└───────────────────────┬───────────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 5 — User I/O Helpers  (Thin, Testable)             │
│  _read_input()              ← encoding-safe on Windows    │
│  _classify_user_intent()    ← keyword rule engine         │
└───────────────────────┬───────────────────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────────────────┐
│  LAYER 6 — Orchestration  (Entry Point)                   │
│  run_chef_agent()                                         │
│  → confirm path: scale → display → consume → exit         │
│  → revise path:  revision loop (max MAX_REVISIONS = 5)    │
│  → cancel path:  exit gracefully                          │
└───────────────────────────────────────────────────────────┘
```

> **Why fetch all active items, not just those expiring soon?**
> An earlier version of the agent queried only items expiring within 14 days. This silently hid frozen meat, pantry staples, and any item with a longer shelf life — causing the LLM to wrongly report those ingredients as missing. `get_all_active_items()` retrieves the full inventory so the LLM has an accurate picture of what is actually in the kitchen. Items are sorted soonest-expiring first so time-sensitive ingredients appear at the top of the prompt.

### Stateful Chat Architecture

A single `google.genai` chat session is created **once** per run via `_create_chef_chat()`. `SYSTEM_INSTRUCTION` — the chef persona contract — is loaded at session creation and persists for the entire conversation. Every `send_message()` call appends to the retained history automatically.

This means revision prompts are minimal:

```python
# Initial turn — sends full inventory + user vibe
_build_initial_prompt(fridge_items, user_vibe)

# Subsequent turns — only the delta
_build_revision_prompt(user_feedback)
# e.g. 'הלקוח ביקש שינוי: "בלי בשר"'
# The session already holds the prior recipe; no re-send needed.
```

#### Defense-in-Depth JSON Extraction (`_extract_json`)

Despite the system instruction mandating raw JSON output, LLMs occasionally wrap responses in markdown fences or prefix them with prose. `_extract_json` handles this with a three-step strategy:

1. Strip all `` ```json `` and ` ``` ` markers with a regex.
2. Locate the first `{` character.
3. Walk forward tracking brace depth to find the exact matching `}`.

If parsing fails entirely, `_parse_recipe_response` returns a `_raw_fallback` dict that keeps the loop alive so the user can retry or exit cleanly — no uncaught exception.

#### Chef Persona Contract (SYSTEM_INSTRUCTION)

The system instruction enforces the following rules at the model level across all turns:

| Rule | Detail |
|---|---|
| **Format** | Raw JSON only — no markdown, no prose before or after |
| **Language** | All text values in Hebrew |
| **Portion control** | Default to exactly one adult serving; never use full available stock |
| **Semantic matching** | Evaluate `category` field, not just `item_name`, before declaring an ingredient missing |
| **Hallucination ban** | Never invent an ingredient not in the provided inventory; use `chef_message` to communicate any gap |
| **Exclusion minimalism** | `excluded_items` only for ingredients the user explicitly requested but couldn't get, or 1–2 notable substitutions — not every unused item |
| **Forbidden language** | Never say: expiry, waste, saving, urgent, תפוגה, בזבוז, לחסוך, דחוף |
| **No memory claims** | Never say the recipe is saved to any app, database, or memory |

### Intent Classification Engine

After each recipe is displayed, the user's freeform Hebrew or English response is classified by a **pure-Python keyword engine** — no LLM call is made. The decision runs in strict priority order:

```
user input (normalised to lowercase)
        │
        ├─ 1. Exact match in _CANCEL_EXACT?
        │      {"לא", "no", "n", "0", "ביי", "bye"}
        │      └→ "cancel"
        │
        ├─ 2. Substring match in _CANCEL_PHRASES?
        │      ["לא צריך", "לא תודה", "תודה רבה", "bye", "exit", ...]
        │      └→ "cancel"
        │
        ├─ 3. Contains _AFFIRM_KEYWORDS substring?
        │      ["כן", "יאללה", "סבבה", "ok", "sure", "yes", ...]
        │      AND NOT contains _CHANGE_KEYWORDS substring?
        │      ["לא", "אבל", "בלי", "שנה", "יותר", "פחות", ...]
        │      └→ "confirm"
        │
        └─ 4. Default
               └→ "revise"
```

The change-keyword override guard is critical: `"כן אבל תעשה יותר קליל"` contains `"כן"` (affirm) **and** `"אבל"` + `"יותר"` (change). Without the guard it would confirm the recipe; with it, it correctly routes to revision.

### Diner Scaling Flow

After the user confirms a recipe, the chef asks how many diners to cook for before deducting inventory. This extra step uses the same stateful chat session — the full recipe is already in history, so only the scaling instruction needs to be sent:

```
User: "כן"
        │
        └→ intent = "confirm"
                │
                ├─ 1. Ask: "לכמה אנשים?" → collect diners_input
                │
                ├─ 2. Send scaling_prompt to LLM (chat session)
                │      "עדכן כמויות עבור {N} סועדים. החזר JSON מלא."
                │
                ├─ 3. _send_and_parse() → scaled_recipe
                │
                ├─ 4. Display scaled recipe
                │
                └─ 5. consume_recipe_items(scaled_recipe.used_fridge_items)
                        (falls back to original recipe if scaling parse fails)
```

**Quantity arithmetic:** `quantity_used` values are treated as floats throughout (`max(1.0, float(...))`) to correctly handle fractional quantities such as `0.25 kg` of meat. After deduction, remainders are rounded to 3 decimal places to prevent floating-point noise (e.g., `2.674 - 1.0 = 1.6739999...` → `1.674`).

### Smart Shopping List Flow

`add_to_smart_list()` is called automatically inside `consume_recipe_items()` whenever an item's quantity reaches zero after cooking. It requires no user action.

```
consume_recipe_items()
        │
        for each used item:
        │
        ├─ Fuzzy-match LLM name → DB item (70 % threshold)
        │
        ├─ remaining = round(current_qty - qty_used, 3)
        │
        ├─ remaining > 0
        │       └→ PATCH fridge_items SET quantity = remaining
        │
        └─ remaining ≤ 0
                ├→ PATCH fridge_items SET status='consumed', quantity=0
                └→ POST smart_shopping_list {item_name, status='pending'}
                         (created_at set automatically by Supabase default)
```

The 70 % fuzzy threshold inside `consume_recipe_items` handles minor name drift that can occur when the LLM returns `"עגבניה"` in `used_fridge_items` but the DB row is stored as `"עגבניות"`.

---

## End-to-End Data Flow

```
Receipt image (JPG)
      │
      ▼  scanner.py
      ├─ Gemini Vision → items[] (name, category, qty, expiry_days)
      ├─ Python → purchase_date=now(), expiry_date=now()+N days
      ├─ Adaptive fuzzy dedup (0.55 or 0.80 threshold)
      └─ Supabase INSERT / soft-delete
                │
                ▼
      fridge_items (status='active')
                │
                ▼  chef_agent.py
      ├─ Fetch ALL active items → filter non-food → sort by expiry
      ├─ User vibe input
      ├─ Gemini Chat (stateful) → recipe JSON
      ├─ Display → user feedback → classify intent
      │
      ├─ revise → scaling prompt in same chat session → loop
      │
      └─ confirm
              ├─ Ask diners → LLM scaling turn → display scaled recipe
              ├─ consume_recipe_items() → PATCH quantities
              └─ add_to_smart_list() for depleted items
                        │
                        ▼
              smart_shopping_list (status='pending')
```

---

## Prerequisites & Setup

**Requirements:** Python 3.11+, a [Google AI Studio](https://aistudio.google.com/) API key, a [Supabase](https://supabase.com/) project.

```bash
pip install google-genai pillow python-dotenv requests
```

Create a `.env` file in the project root (already listed in `.gitignore`):

```env
GOOGLE_API_KEY=your_google_ai_studio_key
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_KEY=your_supabase_anon_or_service_role_key
```

---

## Running the CLI Agents

### Receipt Scanner

```bash
python scanner.py
```

Reads `receipt1.jpg` from the project root by default. To scan a different file:

```python
from scanner import run_scanner
run_scanner("path/to/receipt.jpg")
```

**Sample output:**

```
[14:23:01] Sending receipt to Gemini for analysis...
[14:23:04] Starting smart database synchronization...
INFO: Recent scan detected (3.2m ago). Switching to aggressive deduplication threshold (0.55).
UPDATE: Marked 2 old item(s) as 'consumed'.
SUCCESS: 8 new item(s) saved to your virtual fridge.
INFO: Skipped 1 item(s) — duplicate scan (threshold: 0.55).
```

### Personal Chef Agent

```bash
python chef_agent.py
```

**Sample session:**

```
════════════════════════════════════════════════════════
  Smart Fridge  ·  השף האישי שלך
════════════════════════════════════════════════════════

מה יש לך במטבח עכשיו (9 פריטים):

  ⚠ עגבניות                (2 יח׳  ·  עוד 2 ימים)
    גבינה צהובה             (1 יח׳  ·  עוד 5 ימים)
    ביצים                   (6 יח׳  ·  עוד 9 ימים)
    עוף קפוא                (3 יח׳  ·  עוד 87 ימים)
    ... ועוד 5 פריטים נוספים.

מה אתה רוצה לאכול?
>> ארוחת בוקר קלה

[14:25:10] מכין מתכון בסגנון 'ארוחת בוקר קלה'...

════════════════════════════════════════════════════════
  שקשוקה ביתית עם גבינה
  ביצים בשמן זית עם עגבניות טריות וגבינה מומסת
─── מצרכים ─────────────────────────────────────────────
  מהמקרר:
    • עגבניות  ×1
    • ביצים  ×2
    • גבינה צהובה  ×1
  מהמזווה:
    • שמן זית, מלח, פלפל שחור
─── הוראות הכנה ────────────────────────────────────────
  1.  ...
════════════════════════════════════════════════════════

האם תרצה להכין את זה, או לשנות משהו?
>> כן

[שף]: בחירה מצוינת! לכמה אנשים תרצה שאכין את המנה?
>> 2

[14:26:03] מעדכן כמויות ל-2 סועדים...

─── עדכון מלאי המקרר ───────────────────────────────────────
  ✓  'עגבניות' — נוצל במלואו.
  SHOPPING LIST  →  'עגבניות' נוסף לרשימת הקניות החכמה.
  ✓  'ביצים' — כמות עודכנה ל-2.
  ✓  'גבינה צהובה' — כמות עודכנה ל-0.5.

[שף]: בתיאבון! תהנה מהארוחה.
```

---

## Database Schema

### `fridge_items`

| Column | Type | Set by | Description |
|---|---|---|---|
| `id` | `uuid` / `serial` | Supabase | Primary key |
| `item_name` | `text` | LLM | Normalised Hebrew item name |
| `category` | `text` | LLM | One of seven predefined categories |
| `quantity` | `numeric` | LLM / Python | Current quantity; updated on cooking |
| `purchase_date` | `date` | **Python** | `datetime.now()` at scan time — never the LLM |
| `expiry_date` | `date` | **Python** | `purchase_date + estimated_expiry_days` |
| `status` | `text` | Python | `active` \| `consumed` |
| `created_at` | `timestamptz` | Supabase default | Used by adaptive dedup probe |

### `smart_shopping_list`

| Column | Type | Set by | Description |
|---|---|---|---|
| `item_name` | `text` | Python | Name of the depleted item |
| `created_at` | `timestamptz` | Supabase default | Timestamp of depletion event |
| `status` | `text` | Python | `pending` (default) |

### Item Categories

| Hebrew | Scope |
|---|---|
| `מוצרי חלב וביצים` | Dairy and eggs |
| `בשר ודגים` | Meat, poultry, fish |
| `פירות וירקות` | Fresh produce |
| `מזווה` | Dry pantry goods |
| `נשנושים ומתוקים` | Snacks and sweets |
| `משקאות` | Beverages |
| `אחר` | Deposits, bags, packaging — filtered out before reaching the LLM |
