# Smart-Fridge AI

An AI-native smart fridge proof-of-concept that eliminates food waste through intelligent receipt scanning, virtual inventory management, and conversational recipe generation — powered by Google Gemini 2.5 Flash and Supabase.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [The Two-Layer Design: Python vs. LLM](#the-two-layer-design-python-vs-llm)
4. [scanner.py — Receipt Ingestion Engine](#scannerpy--receipt-ingestion-engine)
5. [chef_agent.py — Personal Chef Engine](#chef_agentpy--personal-chef-engine)
6. [Data Flow](#data-flow)
7. [Prerequisites](#prerequisites)
8. [Environment Setup](#environment-setup)
9. [Running the CLI Agents](#running-the-cli-agents)
10. [Database Schema](#database-schema)

---

## Overview

Smart-Fridge is a two-agent system:

| Agent | Script | Purpose |
|---|---|---|
| **Scanner** | `scanner.py` | Parses a grocery receipt image and upserts items into the virtual fridge inventory |
| **Chef** | `chef_agent.py` | Reads the current inventory and conducts a stateful Hebrew conversation to suggest and refine recipes |

Both agents share the same fundamental design philosophy: **the LLM is only trusted with perception and creativity; all business logic, dates, and I/O are owned by Python.**

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        scanner.py                           │
│                                                             │
│  Receipt Image                                              │
│       │                                                     │
│       ▼                                                     │
│  ┌─────────────┐   JSON items (no dates)   ┌─────────────┐ │
│  │  Gemini LLM │ ─────────────────────────▶│   Python    │ │
│  │  (Vision)   │                           │  Business   │ │
│  └─────────────┘                           │   Logic     │ │
│                                            │             │ │
│                                            │ • purchase_ │ │
│                                            │   date now()│ │
│                                            │ • expiry    │ │
│                                            │   arithmetic│ │
│                                            │ • adaptive  │ │
│                                            │   fuzzy     │ │
│                                            │   dedup     │ │
│                                            └──────┬──────┘ │
│                                                   │        │
│                                                   ▼        │
│                                           Supabase REST API│
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                      chef_agent.py                          │
│                                                             │
│  Supabase REST API                                          │
│       │ active items (Python-filtered)                      │
│       ▼                                                     │
│  ┌─────────────┐                           ┌─────────────┐ │
│  │   Python    │  inventory + user vibe    │  Gemini LLM │ │
│  │  (I/O, DB,  │ ─────────────────────────▶│  (Stateful  │ │
│  │   Display)  │                           │   Chat)     │ │
│  │             │◀─────────────────────────  │             │ │
│  │ • filter    │  structured JSON recipe   │ • recipe    │ │
│  │ • display   │                           │   design    │ │
│  │ • intent    │  revision prompts ──────▶ │ • vibe      │ │
│  │   classify  │◀────────────────────────  │   matching  │ │
│  │ • consume   │  updated JSON recipe      │ • revision  │ │
│  └─────────────┘                           └─────────────┘ │
│       │ PATCH quantity / status                             │
│       ▼                                                     │
│  Supabase REST API + smart_shopping_list                    │
└─────────────────────────────────────────────────────────────┘
```

---

## The Two-Layer Design: Python vs. LLM

A strict responsibility boundary is enforced across both agents. The guiding principle is: **never trust the LLM with anything deterministic.**

### What the LLM is responsible for

| Domain | Details |
|---|---|
| **Vision / OCR** | Reading raw receipt images and extracting item names |
| **Name normalization** | Stripping brand names, weights, and percentages to clean generic Hebrew names |
| **Categorization** | Classifying each item into one of seven predefined Hebrew categories |
| **Expiry estimation** | Estimating storage duration in days based on food type and typical handling (e.g., frozen meat vs. fresh dairy) |
| **Culinary creativity** | Designing recipes that match the user's stated vibe, incorporating available fridge items |
| **Recipe revision** | Adapting a prior recipe to the user's feedback across multiple conversation turns |
| **Semantic ingredient matching** | Resolving user requests like "בשר" (meat) to available fridge items by category, not just string matching |

### What Python is responsible for

| Domain | Details |
|---|---|
| **All date logic** | `purchase_date` is always `datetime.now()`. `expiry_date` is always `purchase_date + timedelta(days=estimated_expiry_days)`. The LLM never touches a date. |
| **Adaptive fuzzy deduplication** | 80% similarity threshold for normal scans; automatically drops to 55% when a re-scan is detected within 15 minutes, collapsing OCR-noise variants |
| **Hebrew plural normalization** | Strips common plural suffixes (`ים`, `ות`, `יות`) before fuzzy comparison so `תפוחים` correctly matches an existing `תפוח` DB entry |
| **Non-food filtering** | Deposits, bags, and packaging items are removed deterministically before any data reaches the LLM |
| **Intent classification** | User responses (`כן`, `לא`, `שנה`) are classified into `confirm / revise / cancel` by a keyword rule engine — no LLM call |
| **JSON extraction** | Defense-in-depth brace-depth parser extracts valid JSON from LLM output regardless of markdown wrapping or trailing prose |
| **DB persistence** | All Supabase reads and writes — including smart upserts, quantity deduction, and shopping list management |
| **Display / UI** | All CLI formatting, ANSI color codes, and output rendering |
| **Loop control** | Revision counter capped at `MAX_REVISIONS = 5` to prevent infinite loops |

---

## scanner.py — Receipt Ingestion Engine

### Layers

| Layer | Type | Responsibility |
|---|---|---|
| **1 — LLM** | Probabilistic | `analyze_receipt()`: sends the image to Gemini, receives an `items` array (no dates) |
| **2 — DB Helpers** | Deterministic | `get_active_items()`, `get_latest_item_timestamp()`, `update_consumed_items()` — thin Supabase REST wrappers |
| **3 — Business Logic** | Deterministic / Pure | `normalize_hebrew_for_matching()`, `detect_scan_mode()`, `find_best_match()`, `build_fridge_rows()` |
| **4 — Orchestration** | Deterministic | `save_to_db()` → `run_scanner()`: the end-to-end entry point |

### Smart Upsert Workflow (`save_to_db`)

1. **Probe** the DB for the most recent insert timestamp and select the adaptive deduplication threshold.
2. **Fetch** the full active inventory.
3. **Set** `purchase_date = datetime.now()` in Python (never delegated to the LLM).
4. For each candidate row, **fuzzy-match** against the active inventory:
   - **Condition A** — same-day match: duplicate receipt scan → skip silently.
   - **Condition B** — older match: restock of an existing item → retire the old row (`status = consumed`).
5. **Batch-insert** new rows; **batch-mark** retired rows as `consumed`.

### Adaptive Deduplication

Scanning the same receipt twice within minutes produces near-identical item names with minor OCR noise. `detect_scan_mode()` checks the age of the most recent DB insert:

- **≤ 15 minutes** → threshold drops to **0.55** (aggressive: collapses OCR variants like `'מלפפון '` vs `'מלפפון'`)
- **> 15 minutes** → threshold stays at **0.80** (standard)

---

## chef_agent.py — Personal Chef Engine

### Layers

| Layer | Type | Responsibility |
|---|---|---|
| **1 — Data Retrieval** | Deterministic | `get_urgent_items()`: queries Supabase, filters non-food items in Python |
| **2 — LLM Chat** | Probabilistic | Stateful `google.genai` chat session; `_send_and_parse()`, `_build_initial_prompt()`, `_build_revision_prompt()` |
| **3 — Recipe Display** | Deterministic | `_format_recipe_for_display()`: renders the structured recipe dict as a Hebrew CLI string |
| **4 — DB Consumption** | Deterministic | `consume_recipe_items()`, `add_to_smart_list()`: deducts quantities and updates Supabase |
| **5 — User I/O** | Deterministic | `_read_input()` (encoding-safe), `_classify_user_intent()` (keyword rule engine) |
| **6 — Orchestration** | Deterministic | `run_chef_agent()`: the conversational loop entry point |

### Stateful Chat Architecture

A single `google.genai` chat session is created once per run. `SYSTEM_INSTRUCTION` — the chef persona contract — is loaded at session creation and persists for the entire conversation. Each `send_message()` call appends to the retained history, so revision requests ("make it lighter", "no meat") naturally build on the previous recipe without re-sending the full fridge inventory every turn.

### Intent Classification (`_classify_user_intent`)

After each recipe is displayed, the user's freeform Hebrew or English response is classified by a pure-Python keyword engine. Decision priority:

1. **Cancel (exact)** — bare words: `"לא"`, `"no"`, `"ביי"`, `"quit"` → exit
2. **Cancel (phrase)** — substrings: `"לא צריך"`, `"תודה רבה"`, `"bye"` → exit
3. **Confirm** — affirmative keyword present (`"כן"`, `"יאללה"`, `"סבבה"`, `"ok"`) **and** no change keyword present → consume items and exit
4. **Revise (default)** — everything else, including mixed signals (`"כן אבל..."`) → revision loop

The change-keyword override guard (`_CHANGE_KEYWORDS`) ensures that `"כן אבל תעשה יותר קליל"` is correctly routed to revision rather than confirmation.

### Conversation Flow

```
run_chef_agent()
      │
      ├─ 1. get_urgent_items()          # Python: fetch + filter
      ├─ 2. collect user vibe           # Python: _read_input()
      ├─ 3. _create_chef_chat()         # LLM: open stateful session
      ├─ 4. _send_and_parse(initial)    # LLM: generate recipe
      │
      └─ loop (max MAX_REVISIONS=5):
            ├─ _format_recipe_for_display()   # Python: render
            ├─ _classify_user_intent()        # Python: classify
            │
            ├─ confirm → consume_recipe_items()  # Python: PATCH DB
            │            add_to_smart_list()     # Python: POST DB
            │            exit
            │
            ├─ cancel  → exit
            │
            └─ revise  → _send_and_parse(revision_prompt)  # LLM
                         loop back
```

---

## Data Flow

```
Receipt image (JPG)
      │
      ▼
scanner.py::analyze_receipt()          ← Gemini Vision (LLM)
      │  items[]: name, category, qty, expiry_days
      ▼
scanner.py::save_to_db()               ← Python
      │  adds purchase_date, computes expiry_date, deduplicates
      ▼
Supabase fridge_items table
      │
      ▼
chef_agent.py::get_urgent_items()      ← Python (REST query)
      │  filters non-food items
      ▼
chef_agent.py::run_chef_agent()        ← Python + Gemini Chat (LLM)
      │  stateful recipe conversation
      ▼
chef_agent.py::consume_recipe_items()  ← Python
      │  deducts quantities, marks consumed
      ▼
Supabase fridge_items (updated)
Supabase smart_shopping_list (depleted items)
```

---

## Prerequisites

- Python 3.11+
- A [Google AI Studio](https://aistudio.google.com/) API key with access to Gemini 2.5 Flash
- A [Supabase](https://supabase.com/) project with the schema described below

Install dependencies:

```bash
pip install google-genai pillow python-dotenv requests
```

---

## Environment Setup

Create a `.env` file in the project root (already listed in `.gitignore`):

```env
GOOGLE_API_KEY=your_google_api_key_here
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_KEY=your_supabase_anon_or_service_role_key
```

---

## Running the CLI Agents

### Receipt Scanner

Scans a receipt image and upserts the extracted items into the virtual fridge:

```bash
python scanner.py
```

By default, the scanner reads `receipt1.jpg` from the project root. To scan a different image, call the entry point directly in Python:

```python
from scanner import run_scanner
run_scanner("path/to/your_receipt.jpg")
```

**Expected output:**

```
[14:23:01] Sending receipt to Gemini for analysis...
[14:23:04] Starting smart database synchronization...
UPDATE: Marked 2 old item(s) as 'consumed'.
SUCCESS: 8 new item(s) saved to your virtual fridge.
INFO: Skipped 1 item(s) — duplicate scan (threshold: 0.8).
```

---

### Personal Chef Agent

Starts an interactive conversational session based on the current fridge inventory:

```bash
python chef_agent.py
```

**Example session:**

```
════════════════════════════════════════════════════════
  Smart Fridge  ·  השף האישי שלך
════════════════════════════════════════════════════════

מה יש לך במטבח עכשיו (6 פריטים):

  ⚠ עגבניות                (2 יח׳  ·  עוד 2 ימים)
    גבינה צהובה             (1 יח׳  ·  עוד 5 ימים)
    ביצים                   (6 יח׳  ·  עוד 9 ימים)
    ...

מה אתה רוצה לאכול? (לדוגמה: קינוח שוקולד, פסטה איטלקית, סלט קל, ארוחת בוקר)
>> ארוחת בוקר קלה

[14:25:10] מכין מתכון בסגנון 'ארוחת בוקר קלה'...

════════════════════════════════════════════════════════
  שקשוקה ביתית עם גבינה
  ביצים בשמן זית עם עגבניות טריות וגבינה מומסת
...
════════════════════════════════════════════════════════

האם תרצה להכין את זה, או לשנות משהו?
>> כן, בתיאבון

─── עדכון מלאי המקרר ───────────────────────────────────────
  ✓  'עגבניות' — נוצל במלואו.
  SHOPPING LIST  →  'עגבניות' נוסף לרשימת הקניות החכמה.
  ✓  'ביצים' — כמות עודכנה ל-4.

[שף]: בתיאבון! תהנה מהארוחה.
```

---

## Database Schema

### `fridge_items`

| Column | Type | Description |
|---|---|---|
| `id` | `uuid` / `serial` | Primary key |
| `item_name` | `text` | Normalized Hebrew item name |
| `category` | `text` | One of the seven predefined categories |
| `quantity` | `integer` | Current quantity in the fridge |
| `purchase_date` | `date` | Set by Python at scan time (`datetime.now()`) |
| `expiry_date` | `date` | Computed by Python (`purchase_date + estimated_expiry_days`) |
| `status` | `text` | `active` \| `consumed` |
| `created_at` | `timestamptz` | Auto-set by Supabase; used for adaptive dedup timing |

### `smart_shopping_list`

| Column | Type | Description |
|---|---|---|
| `item_name` | `text` | Name of the depleted item |
| `added_at` | `timestamptz` | Timestamp of depletion event |
| `status` | `text` | `pending` (default) |
