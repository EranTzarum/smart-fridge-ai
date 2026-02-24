"""
chef_agent.py  —  Smart Fridge Personal Chef Engine

Responsibility boundary (enforced):
  Python (this file): Data retrieval, deterministic non-food filtering,
                      JSON extraction, recipe display, DB quantity updates,
                      smart shopping list management, user I/O, loop control.
  LLM (Gemini):       Culinary creativity — recipe design, vibe matching,
                      per-ingredient fitness evaluation, recipe revision.

Chef persona contract (system-instruction level):
  - Primary goal:    match the user's culinary vibe.
  - Silent rule:     incorporate available fridge items wherever culinarily sound.
  - Forbidden:       "expiry", "waste", "saving", "urgent" — zero food-waste language.
  - Forbidden:       claiming the recipe is saved to memory / app / any system.
  - Output format:   raw JSON only (enforced via system instruction + robust extraction).

Chat architecture:
  A stateful google.genai chat session retains full conversation history, so revision
  requests ("make it lighter", "no meat") naturally build on the previous recipe without
  re-sending the fridge inventory every turn.

Cloud Function ready: call `run_chef_agent()` as the entry point.
"""

import os
import re
import json
import difflib
import requests
from datetime import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
client = genai.Client()


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG  —  Constants & Filter Rules
# ──────────────────────────────────────────────────────────────────────────────

NON_FOOD_CATEGORIES  = {"אחר"}
NON_FOOD_NAME_TOKENS = ["פיקדון", "שקית", "קרטון", "אריזה"]

# Maximum recipe revision rounds before the loop self-terminates
MAX_REVISIONS = 5

# ── Intent classification constants ──────────────────────────────────────────
#
# WHY substrings instead of exact-match sets?
#   Users naturally type multi-word affirmatives: "כן תודה", "יאללה בוא נעשה",
#   "אני מכין את זה עכשיו". Exact-match lookup misses all of these and falls
#   through to the "revise" path, sending the answer to Gemini as recipe feedback.
#
# CHANGE_KEYWORDS act as an override guard:
#   "כן אבל תעשה יותר קליל" contains "כן" (affirm) AND "אבל"+"יותר" (change).
#   The change guard blocks the affirm path → correctly routed to "revise".

# Affirmative signals — checked as substrings of the normalised input.
_AFFIRM_KEYWORDS = [
    "כן", "יאללה", "סבבה", "אני מכין", "מעולה", "מצוין",
    "אחלה", "בסדר", "הולך", "קדימה", "נעשה", "יאה", "טוב",
    "תודה", "ok", "sure", "yes", "y",
]

# Change/negative signals — any match BLOCKS the affirmative path.
# Catches: "כן אבל...", "בלי בשר", "תעשה יותר...", "לא, שנה..."
_CHANGE_KEYWORDS = [
    "לא", "אבל", "לשנות", "בלי", "שנה", "פחות",
    "יותר", "במקום", "אחרת", "רק",
]

# Explicit cancellation phrases — checked as substrings before affirm logic.
_CANCEL_PHRASES = [
    "לא צריך", "לא תודה", "לא, תודה", "תודה רבה",
    "ביי", "bye", "cancel", "exit", "quit",
]

# Bare one-word cancellations — checked as exact matches (highest priority).
_CANCEL_EXACT = {"לא", "no", "n", "0", "ביי", "bye"}


# ──────────────────────────────────────────────────────────────────────────────
# SYSTEM INSTRUCTION  —  Chef Persona (persistent across all chat turns)
#
# Placed at module level so it is easy to audit and version-control independently
# of the conversation logic.
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_INSTRUCTION = """\
You are an elite personal chef with deep culinary expertise, creative instincts, \
and an intuitive feel for flavor. You are having a live conversation with a client \
about what to cook right now.

RESPONSE FORMAT — MANDATORY:
You MUST always respond with a raw, valid JSON object and nothing else.
No markdown fences, no ```json, no text before or after the JSON. Just the object.

Required schema (all text values must be written in Hebrew):
{
  "chef_message": "הודעה קצרה מהשף — ראה כללים מפורטים להלן",
  "recipe_name": "שם המתכון",
  "tagline": "משפט קצר ומפתה שמתאר את המנה",
  "used_fridge_items": [
    {"item_name": "שם מדויק כפי שמופיע ברשימה", "quantity_used": number}
  ],
  "excluded_items": [
    {"item_name": "שם", "reason": "סיבה קולינרית קצרה"}
  ],
  "pantry_staples_needed": ["מלח", "שמן זית", "פלפל שחור"],
  "instructions": ["שלב 1...", "שלב 2..."]
}

━━━ CHEF MESSAGE RULES (chef_message field) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The chef_message field is the ONLY approved channel for communicating inventory
gaps to the client. Use it as follows:

• MISSING ingredient (no equivalent in inventory):
  Honestly inform the client what is missing and what you made instead.
  Example: "ביקשת בשר, אבל אין לנו כרגע במלאי. הכנתי לך מנה מושלמת עם ביצים."

• REQUEST FULFILLED (directly or via semantic equivalent):
  Write a brief, welcoming or creative sentence about the dish, OR leave it
  as an empty string "".
  Example: "מצאתי עוף מצוין במטבח — בדיוק מה שצריך לארוחה הזאת."

CRITICAL: NEVER invent or hallucinate ingredients not in the provided inventory.
          chef_message is the sole outlet for stating what you cannot cook.

━━━ SEMANTIC MATCHING RULE (apply BEFORE claiming any ingredient is missing) ━━
When the client requests a food type or category, evaluate BOTH the item_name AND
the category field of each inventory item using culinary logic — not string matching.

Category equivalence — treat these as valid matches for user requests:
  "בשר" / "עוף" / "דגים" / "חלבון"  →  ANY item in category "בשר ודגים"
                                          (e.g., עוף, דג, בקר, קציצות, סלמון)
  "חלבי" / "גבינה" / "יוגורט"       →  ANY item in category "מוצרי חלב וביצים"
  "ירקות" / "טרי" / "סלט"           →  ANY item in category "פירות וירקות"
  "מתוק" / "קינוח" / "עוגה"         →  items in "נשנושים ומתוקים" OR dairy items
  "פסטה" / "קטניות" / "דגנים"       →  ANY item in category "מזווה"

Only declare an ingredient missing if NO item in the inventory — by name or by
category — can serve as a culinary equivalent for what the client requested.

━━━ PORTION CONTROL — MANDATORY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
By default, generate ALL recipes scaled for EXACTLY ONE average adult serving.
NEVER use the entire available inventory if it exceeds a normal single portion.
Use realistic culinary portion sizes per person:
  - Meat / poultry / fish : ~150–200 g  (≈ 0.2 units if listed by kg)
  - Fresh vegetables       : 1–2 items or ~100–150 g
  - Dairy (milk, cream)    : ~50–100 ml
  - Dry pasta / grains     : ~80 g
  - Eggs                   : 1–2 units

The quantity_used values in used_fridge_items MUST reflect a realistic SINGLE
portion — never the full available stock. Using 3 kg of chicken for one person
is forbidden. If the client later requests scaling for more diners, you will
receive an explicit follow-up message asking you to update the quantities.

━━━ EXCLUSION RULE (excluded_items field) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The excluded_items array MUST be minimal. ONLY populate it in these two cases:
  1. The user specifically requested an ingredient or dish type that you could
     not deliver (e.g., user asked for "pasta" but there is none in inventory).
  2. You made 1–2 significant culinary substitutions that the client should know
     about (e.g., used turkey instead of beef for a dietary reason).

DO NOT explain why you skipped unrelated items that nobody asked for.
Example of what is FORBIDDEN: listing "בננה — לא מתאים לתבשיל עוף" when the
user asked for a chicken stew. That is obvious — omit it entirely.
If there are no notable exclusions or substitutions, return an empty array: [].

━━━ ABSOLUTE RULES — NEVER VIOLATE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. All text values in the JSON must be in Hebrew.
2. Never use the words or concepts: expiry, waste, saving ingredients, urgent,
   תפוגה, בזבוז, לחסוך, דחוף. Treat available ingredients as "what's in the kitchen".
3. NEVER claim you are saving, storing, or remembering the recipe in any app,
   memory, database, or external system. You are a chef — you only cook.
4. NEVER make promises or statements about what will happen after this conversation.
5. NEVER invent ingredients not present in the provided inventory.
   Use chef_message to communicate any gap — never silently hallucinate a substitute.
6. When the client requests changes, adapt the recipe fully and return the complete
   updated JSON — never a partial diff.
7. Be a chef: focus on taste, texture, technique, and the dining experience.\
"""


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 1 — Data Retrieval  (Deterministic)
# ──────────────────────────────────────────────────────────────────────────────

def _build_headers(supabase_key: str, extra: dict = None) -> dict:
    """Construct standard Supabase REST API headers."""
    headers = {
        "apikey":        supabase_key,
        "Authorization": f"Bearer {supabase_key}",
    }
    if extra:
        headers.update(extra)
    return headers


def _is_food_item(item: dict) -> bool:
    """
    Pure Python guard — returns False for non-food items that must never reach the LLM.
    Catches deposits, bags, packaging, and anything in the 'אחר' category.
    """
    if item.get("category") in NON_FOOD_CATEGORIES:
        return False
    if any(token in item.get("item_name", "") for token in NON_FOOD_NAME_TOKENS):
        return False
    return True


def get_all_active_items() -> list[dict]:
    """
    Fetches ALL active fridge items regardless of expiry date.

    Previous behaviour (get_urgent_items) applied &expiry_date=lte.{target_date},
    which silently hid frozen meat, pantry staples, and any item expiring beyond
    the 14-day window — making the LLM claim those ingredients didn't exist.

    The full active inventory is returned so the LLM has an accurate picture of
    what is actually in the kitchen. Items are sorted by expiry_date ascending so
    the most time-sensitive ingredients appear first in the prompt.

    Non-food line items (deposits, bags, 'אחר' category) are stripped in Python
    before the list reaches the LLM.
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching full active inventory...")

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("ERROR: Supabase credentials missing.")
        return []

    endpoint = (
        f"{supabase_url}/rest/v1/fridge_items"
        f"?select=id,item_name,category,quantity,expiry_date"
        f"&status=eq.active"
    )

    try:
        response = requests.get(endpoint, headers=_build_headers(supabase_key))
        response.raise_for_status()
        all_items = response.json()

        food_items     = [item for item in all_items if _is_food_item(item)]
        filtered_count = len(all_items) - len(food_items)
        if filtered_count:
            print(f"INFO: Filtered out {filtered_count} non-food item(s) before recipe generation.")

        # Sort soonest-expiring first so the LLM naturally prioritises them
        food_items.sort(key=lambda x: x["expiry_date"])
        return food_items

    except Exception as e:
        print(f"DATABASE ERROR: {e}")
        return []


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 2 — LLM Chat  (Probabilistic / Generative)
#
# Architecture: a single stateful google.genai chat session is created once per
# run. SYSTEM_INSTRUCTION is loaded once at session creation and persists across
# all turns. Each send_message() call appends to the retained history, so
# revision requests ("make it lighter") implicitly reference the prior recipe.
# ──────────────────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> str:
    """
    Robustly extracts the first complete JSON object from an LLM response string.

    Strategy (defense-in-depth):
      1. Strip all markdown code fences (```json ... ``` and plain ```) using regex.
      2. Locate the first opening brace '{'.
      3. Walk forward tracking brace depth to find the matching closing brace.

    This handles: markdown-wrapped JSON, leading prose, trailing notes, and any
    whitespace variation — without relying on the LLM obeying format instructions.

    Raises ValueError if no complete JSON object is found.
    """
    # Remove all markdown code fence markers
    cleaned = re.sub(r'```(?:json)?\s*', '', text)
    cleaned = cleaned.replace('`', '').strip()

    start = cleaned.find('{')
    if start == -1:
        raise ValueError("No JSON object ('{') found in LLM response.")

    depth = 0
    for i, ch in enumerate(cleaned[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return cleaned[start : i + 1]

    raise ValueError("LLM response contains an unclosed JSON object (brace mismatch).")


def _parse_recipe_response(raw_text: str) -> dict:
    """
    Extracts and parses the JSON recipe from an LLM response.
    Always returns a dict — falls back to a raw-text container on parse failure
    so the calling loop can display something and either retry or exit cleanly.
    """
    try:
        json_str = _extract_json(raw_text)
        return json.loads(json_str)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"WARNING: Could not parse chef's response as JSON ({e}). Displaying raw text.")
        return {
            "chef_message":          "",
            "recipe_name":           "מתכון",
            "tagline":               "",
            "used_fridge_items":     [],
            "excluded_items":        [],
            "pantry_staples_needed": [],
            "instructions":          [raw_text],
            "_raw_fallback":          True,
        }


def _build_initial_prompt(fridge_items: list[dict], user_vibe: str) -> str:
    """
    Constructs the opening message that starts the chef conversation.

    The category field is explicitly included alongside each item name so the LLM
    can apply the SEMANTIC MATCHING RULE (e.g., map "בשר" → category "בשר ודגים")
    rather than relying on literal string comparison of item names.
    """
    items_block = "\n".join(
        f"- {item['item_name']}  "
        f"(כמות: {item['quantity']}, קטגוריה: {item.get('category', 'לא ידוע')})"
        for item in fridge_items
    )
    return (
        f"המרכיבים הזמינים במטבח כרגע:\n{items_block}\n\n"
        f"הלקוח מחפש: \"{user_vibe}\"\n\n"
        "לפני שאתה מחליט שמרכיב חסר, החל את כלל ה-SEMANTIC MATCHING: "
        "בדוק את שדה הקטגוריה של כל מרכיב ולא רק את שמו. "
        "צור מתכון מעולה שמשקף בדיוק את הבקשה ושלב את המרכיבים הזמינים בצורה טבעית. "
        "החזר JSON בלבד."
    )


def _build_revision_prompt(user_feedback: str) -> str:
    """
    Wraps the user's freeform feedback as a revision instruction for the chat session.
    The session history already contains the previous recipe — no need to re-send it.
    """
    return (
        f"הלקוח ביקש שינוי: \"{user_feedback}\"\n\n"
        "עדכן את המתכון בהתאם. החזר את ה-JSON המלא והמעודכן."
    )


def _create_chef_chat():
    """
    Creates a stateful Gemini chat session primed with the chef persona.
    SYSTEM_INSTRUCTION is loaded once and persists for the entire conversation.
    Conversation history is retained automatically between send_message() calls.
    """
    return client.chats.create(
        model='gemini-2.5-flash',
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
        ),
    )


def _send_and_parse(chat, message: str) -> dict:
    """Sends one message to the active chat session and returns a parsed recipe dict."""
    response = chat.send_message(message)
    return _parse_recipe_response(response.text)


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 3 — Recipe Display  (Deterministic)
# Python owns all display logic — the LLM only generates structured data.
# ──────────────────────────────────────────────────────────────────────────────

# ANSI escape codes for terminal styling.
# These work on Windows Terminal, PowerShell 7+, macOS, and Linux.
# Fallback: if the terminal strips them, the text still renders — just unstyled.
_ANSI_YELLOW_BOLD = "\033[1;93m"
_ANSI_RESET       = "\033[0m"


def _format_recipe_for_display(recipe: dict) -> str:
    """
    Formats the structured recipe dict into a clean, readable Hebrew CLI string.

    Rendering order:
      1. chef_message  — printed first in bold yellow when the chef has a note
                         about inventory gaps or a welcoming sentence.
      2. recipe_name + tagline
      3. excluded_items  (הערת השף — culinary-exclusion notes)
      4. ingredients  (מהמקרר + מהמזווה)
      5. instructions
    """
    lines = []

    # ── 1. Chef message — inventory-gap notice or welcoming note ──────────────
    chef_msg = recipe.get("chef_message", "").strip()
    if chef_msg:
        lines.append(
            _ANSI_YELLOW_BOLD
            + "─── הודעה מהשף ────────────────────────────────────────"
            + _ANSI_RESET
        )
        lines.append(_ANSI_YELLOW_BOLD + f"  {chef_msg}" + _ANSI_RESET)
        lines.append("")

    # ── 2. Recipe name + tagline ───────────────────────────────────────────────
    lines.append(f"  {recipe['recipe_name']}")
    if recipe.get("tagline"):
        lines.append(f"  {recipe['tagline']}")
    lines.append("")

    # ── 3. Culinary exclusion notes ───────────────────────────────────────────
    excluded = recipe.get("excluded_items", [])
    if excluded:
        lines.append("─── הערת השף ───────────────────────────────────────────")
        for exc in excluded:
            lines.append(f"  {exc.get('item_name', '')}:  {exc.get('reason', '')}")
        lines.append("")

    # ── 4. Ingredients ────────────────────────────────────────────────────────
    lines.append("─── מצרכים ─────────────────────────────────────────────")
    used = recipe.get("used_fridge_items", [])
    if used:
        lines.append("  מהמקרר:")
        for item in used:
            lines.append(f"    • {item['item_name']}  ×{item.get('quantity_used', 1)}")

    staples = recipe.get("pantry_staples_needed", [])
    if staples:
        lines.append("  מהמזווה:")
        for s in staples:
            lines.append(f"    • {s}")

    # ── 5. Instructions ───────────────────────────────────────────────────────
    lines.append("")
    lines.append("─── הוראות הכנה ────────────────────────────────────────")
    for i, step in enumerate(recipe.get("instructions", []), 1):
        lines.append(f"  {i}.  {step}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 4 — DB Consumption  (Deterministic)
# ──────────────────────────────────────────────────────────────────────────────

def _patch_fridge_item(supabase_url: str, supabase_key: str, item_id: str, patch_data: dict) -> None:
    """Generic PATCH for a single fridge item by ID."""
    endpoint = f"{supabase_url}/rest/v1/fridge_items?id=eq.{item_id}"
    headers  = _build_headers(supabase_key, {"Content-Type": "application/json"})
    response = requests.patch(endpoint, json=patch_data, headers=headers)
    response.raise_for_status()


def add_to_smart_list(supabase_url: str, supabase_key: str, item_name: str) -> None:
    """
    Adds a depleted item to the smart_shopping_list table.
    Triggered automatically when a fridge item's quantity reaches zero after cooking.

    Target table schema:
      item_name (text), created_at (timestamptz, default NOW()), status (text, default 'pending')
    """
    endpoint = f"{supabase_url}/rest/v1/smart_shopping_list"
    payload  = {
        "item_name": item_name,
        "status":    "pending",
    }
    headers = _build_headers(supabase_key, {
        "Content-Type": "application/json",
        "Prefer":       "return=minimal",
    })
    response = requests.post(endpoint, json=payload, headers=headers)
    response.raise_for_status()
    print(f"  SHOPPING LIST  →  '{item_name}' נוסף לרשימת הקניות החכמה.")


def consume_recipe_items(
    supabase_url: str,
    supabase_key: str,
    used_items: list[dict],
    fridge_items: list[dict],
) -> None:
    """
    Updates fridge DB quantities based on what was cooked.

    For each item in used_items:
      - Deducts quantity_used from the current DB quantity.
      - remaining <= 0: marks status='consumed', quantity=0, adds to smart_shopping_list.
      - remaining > 0:  updates quantity field only.

    Uses the fridge_items list already in memory (no extra DB reads).
    Fuzzy matching (70% threshold) handles minor LLM name drift.
    """
    if not supabase_url or not supabase_key:
        print("ERROR: Supabase credentials missing. Cannot update inventory.")
        return

    fridge_by_name = {item["item_name"]: item for item in fridge_items}
    print("\n─── עדכון מלאי המקרר ───────────────────────────────────────")

    for used in used_items:
        name     = used.get("item_name", "").strip()
        # Float arithmetic: quantities can be fractional (e.g., 0.25 kg of meat).
        # int() was truncating decimals and breaking partial-deduction tracking.
        qty_used = max(1.0, float(used.get("quantity_used", 1.0)))

        db_item = fridge_by_name.get(name)
        if not db_item:
            close = difflib.get_close_matches(
                name, list(fridge_by_name.keys()), n=1, cutoff=0.70
            )
            if close:
                db_item = fridge_by_name[close[0]]
                print(f"  INFO: התאמה מקורבת  '{name}'  →  '{close[0]}'")
            else:
                print(f"  WARNING: לא נמצאה התאמה ל-'{name}' בנתוני המקרר. מדלג.")
                continue

        item_id       = db_item["id"]
        current_qty   = float(db_item.get("quantity", 1.0))
        # round() prevents floating-point noise (e.g., 2.674 - 1.0 = 1.6739999...)
        remaining_qty = round(current_qty - qty_used, 3)

        try:
            if remaining_qty <= 0:
                _patch_fridge_item(supabase_url, supabase_key, item_id, {
                    "status":   "consumed",
                    "quantity": 0,
                })
                print(f"  ✓  '{db_item['item_name']}' — נוצל במלואו.")
                add_to_smart_list(supabase_url, supabase_key, db_item["item_name"])
            else:
                _patch_fridge_item(supabase_url, supabase_key, item_id, {
                    "quantity": remaining_qty,
                })
                print(f"  ✓  '{db_item['item_name']}' — כמות עודכנה ל-{remaining_qty}.")
        except Exception as e:
            print(f"  DB ERROR בעדכון '{db_item['item_name']}': {e}")


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 5 — User I/O Helpers  (Thin, Testable)
# ──────────────────────────────────────────────────────────────────────────────

def _read_input(prompt: str) -> str:
    """
    Reads user input with graceful handling for encoding issues on Windows
    terminals when Hebrew characters are involved.
    """
    try:
        return input(prompt).strip()
    except (UnicodeDecodeError, EOFError):
        return ""


def _classify_user_intent(answer: str) -> str:
    """
    Classifies a freeform Hebrew/English response into one of three intents.

    Decision order (matters — each step can short-circuit the rest):

      1. CANCEL (exact)   — bare single-word cancellation: "לא", "ביי", "no".
      2. CANCEL (phrase)  — explicit cancel phrases: "לא צריך", "תודה רבה", "bye".
      3. CONFIRM          — input contains an affirmative keyword AND contains
                            NO change/negative keyword.
                            "כן תודה"          → affirm="כן",  no change → confirm
                            "יאללה בוא נעשה"   → affirm="יאללה", no change → confirm
                            "כן אבל בלי בצל"   → affirm="כן",  change="אבל"+"בלי" → revise
      4. REVISE (default) — everything else, including mixed signals and open
                            modification requests: "יותר קליל", "בלי בשר", "שנה ל...".

    Returns: 'confirm' | 'cancel' | 'revise'
    """
    normalized = answer.strip().lower()

    # Step 1 — bare exact-match cancellations
    if normalized in _CANCEL_EXACT:
        return "cancel"

    # Step 2 — cancel phrases (substring search)
    if any(phrase in normalized for phrase in _CANCEL_PHRASES):
        return "cancel"

    # Step 3 — affirmative, but only when no change keyword overrides it
    has_affirm = any(kw in normalized for kw in _AFFIRM_KEYWORDS)
    has_change = any(kw in normalized for kw in _CHANGE_KEYWORDS)

    if has_affirm and not has_change:
        return "confirm"

    # Step 4 — default: treat as a recipe revision request
    return "revise"


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 6 — Orchestration  (Entry Point)
# ──────────────────────────────────────────────────────────────────────────────

def run_chef_agent() -> None:
    """
    Cloud Function entry point.

    Conversation flow:
      1.  Fetch & filter fridge items.
      2.  Collect user vibe.
      3.  Open a stateful Gemini chat session (SYSTEM_INSTRUCTION loaded once).
      4.  Generate initial recipe via _send_and_parse().
      5.  Display the formatted recipe.
      6.  Ask: "האם תרצה להכין את זה, או לשנות משהו?"
          ┌─ confirm → consume_recipe_items() → exit.
          ├─ revise  → _build_revision_prompt() → _send_and_parse() → loop to step 5.
          └─ cancel  → exit gracefully.
      7.  Revision counter capped at MAX_REVISIONS to prevent infinite loops.
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")

    active_items = get_all_active_items()
    if not active_items:
        print("\n[שף]: המקרר ריק — אין פריטים פעילים במלאי.")
        return

    print("\n" + "═" * 56)
    print("  Smart Fridge  ·  השף האישי שלך")
    print("═" * 56)

    today = datetime.now()
    print(f"\nמה יש לך במטבח עכשיו ({len(active_items)} פריטים):\n")
    for item in active_items[:7]:
        days_left    = (datetime.strptime(item["expiry_date"], "%Y-%m-%d") - today).days
        urgency_flag = "⚠ " if days_left <= 3 else "  "
        print(
            f"  {urgency_flag}{item['item_name']:22s}"
            f"  ({item['quantity']} יח׳  ·  עוד {days_left} ימים)"
        )
    if len(active_items) > 7:
        print(f"  ... ועוד {len(active_items) - 7} פריטים נוספים.")

    print("\nמה אתה רוצה לאכול? (לדוגמה: קינוח שוקולד, פסטה איטלקית, סלט קל, ארוחת בוקר)")
    user_vibe = _read_input(">> ")
    if not user_vibe:
        user_vibe = "ארוחת ערב ביתית"
        print(f"[שף]: מניח '{user_vibe}'.")

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] מכין מתכון בסגנון '{user_vibe}'...\n")

    # ── Open a stateful chat session — one session per entire conversation ─────
    chat = _create_chef_chat()

    try:
        recipe = _send_and_parse(chat, _build_initial_prompt(active_items, user_vibe))
    except Exception as e:
        print(f"AI ERROR: {e}")
        return

    # ── Conversational refinement loop ────────────────────────────────────────
    revisions = 0

    while True:
        print("\n" + "═" * 56)
        print(_format_recipe_for_display(recipe))
        print("═" * 56)

        # ── Handle parse failures gracefully ──────────────────────────────────
        if recipe.get("_raw_fallback"):
            print("\n[שף]: לא הצלחתי ליצור מתכון מסודר. תאר שוב מה תרצה:")
            feedback = _read_input(">> ")
            if not feedback:
                print("[שף]: בסדר, ניפגש בפעם אחרת.")
                return
            intent = "revise"

        # ── Normal flow: ask confirm / revise / cancel ─────────────────────────
        else:
            print("\nהאם תרצה להכין את זה, או לשנות משהו?")
            print("(לדוגמה: 'כן', 'לא, תעשה את זה יותר קליל', 'בלי בשר', 'לא צריך, תודה')")
            feedback = _read_input(">> ")
            intent   = _classify_user_intent(feedback)

        # ── Branch on intent ──────────────────────────────────────────────────

        if intent == "confirm":
            if recipe.get("_raw_fallback"):
                print("\n[שף]: לא ניתן לעדכן מלאי — המתכון לא הוחזר בפורמט מסודר.")
                return

            # ── Step 1: Ask number of diners ──────────────────────────────────
            print("\n[שף]: בחירה מצוינת! לכמה אנשים תרצה שאכין את המנה?")
            diners_input = _read_input(">> ").strip()
            if not diners_input:
                diners_input = "1"

            print(
                f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                f"מעדכן כמויות ל-{diners_input} סועדים..."
            )

            # ── Step 2: Ask the LLM to scale all quantities ───────────────────
            # The chat session already holds the full recipe history, so we only
            # need to send the scaling instruction — no need to re-describe the dish.
            scaling_prompt = (
                f"הלקוח אישר את המתכון. אנא עדכן את כל הכמויות במתכון עבור "
                f"{diners_input} סועדים. "
                "ודא שסך הכמויות לא חורג מהמלאי הזמין. "
                "החזר את ה-JSON המלא והמעודכן."
            )
            try:
                scaled_recipe = _send_and_parse(chat, scaling_prompt)
            except Exception as e:
                print(f"AI ERROR בעדכון כמויות: {e}. משתמש בכמויות המקוריות.")
                scaled_recipe = recipe

            # ── Step 3: Display the scaled recipe ─────────────────────────────
            print("\n" + "═" * 56)
            print(_format_recipe_for_display(scaled_recipe))
            print("═" * 56)

            # ── Step 4: Deduct the scaled quantities from the DB ──────────────
            # If scaling produced a valid structured recipe use it; otherwise fall
            # back to the original recipe's quantities to avoid a silent no-op.
            source_recipe = scaled_recipe if not scaled_recipe.get("_raw_fallback") else recipe
            used_items    = source_recipe.get("used_fridge_items", [])
            if not used_items:
                print("\n[שף]: לא זוהו מרכיבים ספציפיים מהמקרר — מלאי לא עודכן.")
            else:
                consume_recipe_items(supabase_url, supabase_key, used_items, active_items)

            print("\n[שף]: בתיאבון! תהנה מהארוחה.")
            return

        if intent == "cancel":
            print("\n[שף]: בסדר גמור. בתיאבון בפעם הבאה!")
            return

        # intent == "revise"
        revisions += 1
        if revisions > MAX_REVISIONS:
            print(
                f"\n[שף]: הגענו ל-{MAX_REVISIONS} עדכונים — "
                "נסה להתחיל מחדש עם בקשה חדשה."
            )
            return

        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] "
            f"מעדכן את המתכון ({revisions}/{MAX_REVISIONS})..."
        )
        try:
            recipe = _send_and_parse(chat, _build_revision_prompt(feedback))
        except Exception as e:
            print(f"AI ERROR: {e}")
            return


if __name__ == "__main__":
    run_chef_agent()
