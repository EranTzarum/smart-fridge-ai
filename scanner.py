"""
scanner.py  —  Smart Fridge Ingestion Engine

Responsibility boundary (enforced):
  LLM  (Gemini): Vision / NLP  — item recognition, name normalization,
                                  categorization, expiry-days estimation.
  Python (this):  ALL deterministic logic — purchase date, expiry arithmetic,
                  adaptive fuzzy deduplication, Hebrew normalization, DB persistence.

Deduplication strategy:
  - Normal scan   → 80% similarity threshold.
  - Re-scan within 15 min (OCR noise) → 55% threshold (aggressive dedup).
  - Hebrew plural normalization applied before all comparisons.

Cloud Function ready: call `run_scanner(image_path)` as the entry point.
"""

import os
import json
import time
import difflib
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from google import genai
import PIL.Image

load_dotenv()
client = genai.Client()


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 1 — LLM  (Probabilistic / Generative)
# Scope: item recognition, normalization, categorization, expiry estimation.
# Explicitly out of scope: dates of any kind.
# ──────────────────────────────────────────────────────────────────────────────

def analyze_receipt(image_path: str) -> dict:
    """
    Sends a receipt image to Gemini and returns a parsed dict of food items.

    The LLM returns ONLY the `items` array — no dates, ever.
    All date/expiry-date logic is owned by Python downstream.
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Sending receipt to Gemini for analysis...")
    img = PIL.Image.open(image_path)

    prompt = """
    You are the vision engine for 'Smart-Fridge'. Analyze the attached grocery receipt.

    CRITICAL INSTRUCTIONS:

    1. AGGREGATE: If the same item appears more than once, combine into ONE object
       and SUM the quantities.

    2. MASTER CATALOG MAPPING (normalize item names):
       Strip brand names, weights, and percentages — return a clean generic Hebrew name.
       Examples:
         "קרם גבינה 500 ג 5%"  →  "קרם גבינה"
         "מלבפון ישראל"         →  "מלפפון"
         "חלב טרה 3% 1 ליטר"   →  "חלב"

    3. CATEGORIZE: Assign exactly ONE category from this list:
       "מוצרי חלב וביצים", "בשר ודגים", "פירות וירקות",
       "מזווה", "נשנושים ומתוקים", "משקאות", "אחר"

       Deposits ("פיקדון"), bags ("שקית"), and packaging fees MUST be "אחר".

    4. EXPIRY ESTIMATION (storage-aware, in days):
       - Fresh meat / poultry / fish  → assume user FREEZES  → 90–120 days
       - Dry pantry goods (pasta, sugar, canned goods)       → 365 days
       - Fresh dairy (milk, cottage, yogurt)                 → 5–14 days
       - Fresh vegetables / fruits                           → 5–10 days

    Return ONLY a valid JSON object — no markdown, no extra text:
    {
        "items": [
            {
                "item_name": "string (normalized Hebrew name)",
                "category": "string (from the list above)",
                "quantity": number,
                "estimated_expiry_days": number
            }
        ]
    }
    """

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, img]
            )
            raw = response.text.replace('```json', '').replace('```', '').strip()
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Gemini returned invalid JSON: {e}\nRaw output: {response.text}")
        except Exception as e:
            if ("503" in str(e) or "UNAVAILABLE" in str(e)) and attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"WARNING: API overloaded. Retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                raise


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 2 — DB Helpers  (Deterministic / I/O)
# ──────────────────────────────────────────────────────────────────────────────

def _build_headers(supabase_key: str, extra: dict = None) -> dict:
    """Construct standard Supabase REST API headers."""
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
    }
    if extra:
        headers.update(extra)
    return headers


def get_active_items(supabase_url: str, supabase_key: str) -> list[dict]:
    """Return all currently active fridge items (id, name, purchase_date)."""
    endpoint = (
        f"{supabase_url}/rest/v1/fridge_items"
        f"?status=eq.active&select=id,item_name,purchase_date"
    )
    response = requests.get(endpoint, headers=_build_headers(supabase_key))
    response.raise_for_status()
    return response.json()


def get_latest_item_timestamp(supabase_url: str, supabase_key: str) -> datetime | None:
    """
    Returns the created_at timestamp of the most recently inserted active item.
    Used by detect_scan_mode to identify rapid re-scan scenarios.
    Fails silently — if the column is absent or the query fails, dedup proceeds
    at the standard threshold.
    """
    endpoint = (
        f"{supabase_url}/rest/v1/fridge_items"
        f"?status=eq.active&select=created_at&order=created_at.desc&limit=1"
    )
    try:
        response = requests.get(endpoint, headers=_build_headers(supabase_key))
        response.raise_for_status()
        items = response.json()
        if items and items[0].get("created_at"):
            return datetime.fromisoformat(items[0]["created_at"].replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def update_consumed_items(supabase_url: str, supabase_key: str, item_ids: list) -> None:
    """Soft-delete: mark a list of fridge items as 'consumed'."""
    if not item_ids:
        return
    ids_string = ",".join(str(i) for i in item_ids)
    endpoint = f"{supabase_url}/rest/v1/fridge_items?id=in.({ids_string})"
    headers = _build_headers(supabase_key, {"Content-Type": "application/json"})
    response = requests.patch(endpoint, json={"status": "consumed"}, headers=headers)
    response.raise_for_status()


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 3 — Business Logic  (Deterministic / Pure)
# ──────────────────────────────────────────────────────────────────────────────

def normalize_hebrew_for_matching(name: str) -> str:
    """
    Lightweight Hebrew normalization for deduplication comparison — never stored.
    Strips common plural suffixes so near-identical forms match each other:
      'תפוחים'  →  'תפוח'
      'עגבניות' →  'עגבני'
      'ביצים'   →  'ביצ'

    Suffixes are stripped longest-first to prevent partial stripping
    (e.g., 'יות' must be tried before 'ות').
    """
    name = name.strip()
    for suffix in ["יות", "ים", "ות"]:
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            name = name[:-len(suffix)]
            break
    return name


def detect_scan_mode(latest_ts: datetime | None, window_minutes: int = 15) -> float:
    """
    Adaptive threshold selection based on scan recency.

    Problem: Scanning the same receipt twice within minutes produces near-identical
    item names with minor OCR noise (e.g., 'מלפפון' vs 'מלפפון '). At 80% threshold
    these are treated as new items, generating duplicates.

    Solution: If the most recently inserted item is within `window_minutes`, lower
    the threshold to 55% to aggressively collapse OCR-noise variants.

    Returns:
      0.55 — aggressive mode (recent re-scan within window)
      0.80 — standard mode
    """
    if latest_ts is None:
        return 0.80

    now = datetime.now(timezone.utc)
    if latest_ts.tzinfo is None:
        latest_ts = latest_ts.replace(tzinfo=timezone.utc)

    age_minutes = (now - latest_ts).total_seconds() / 60
    if age_minutes <= window_minutes:
        print(
            f"INFO: Recent scan detected ({age_minutes:.1f}m ago). "
            f"Switching to aggressive deduplication threshold (0.55)."
        )
        return 0.55

    return 0.80


def find_best_match(target_name: str, active_items_dict: dict, threshold: float = 0.80) -> dict | None:
    """
    Fuzzy match with Hebrew normalization on both sides.

    Normalizes target and all candidate keys before difflib comparison,
    then maps the winning normalized key back to the original DB row.
    This ensures 'תפוחים' matches an existing 'תפוח' entry.
    """
    normalized_target = normalize_hebrew_for_matching(target_name)

    # Build normalized → original mapping, preserving the original key for lookup
    normalized_to_original = {
        normalize_hebrew_for_matching(k): k for k in active_items_dict.keys()
    }

    matches = difflib.get_close_matches(
        normalized_target, list(normalized_to_original.keys()), n=1, cutoff=threshold
    )
    if matches:
        original_key = normalized_to_original[matches[0]]
        return active_items_dict[original_key]
    return None


def build_fridge_rows(llm_items: list[dict], purchase_date: datetime) -> tuple[list[dict], list[str]]:
    """
    Converts LLM item dicts into DB-ready rows using Python-owned dates.

    Rules enforced here — never delegated to the LLM:
      - purchase_date = datetime.now()  (passed in by the caller)
      - expiry_date   = purchase_date + timedelta(days=estimated_expiry_days)

    Returns:
      rows          — list of dicts ready for INSERT into fridge_items
      skipped_names — items with no valid expiry estimate (logged, not inserted)
    """
    purchase_date_str = purchase_date.strftime("%Y-%m-%d")
    rows: list[dict] = []
    skipped_names: list[str] = []

    for item in llm_items:
        expiry_days = item.get("estimated_expiry_days")
        if not expiry_days or expiry_days <= 0:
            skipped_names.append(item.get("item_name", "unknown"))
            continue

        expiry_date = purchase_date + timedelta(days=expiry_days)

        rows.append({
            "item_name":     item["item_name"],
            "category":      item["category"],
            "quantity":      item["quantity"],
            "purchase_date": purchase_date_str,
            "expiry_date":   expiry_date.strftime("%Y-%m-%d"),
            "status":        "active",
        })

    return rows, skipped_names


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 4 — Orchestration  (Entry Point)
# ──────────────────────────────────────────────────────────────────────────────

def save_to_db(llm_payload: dict) -> None:
    """
    Smart upsert workflow:
      1. Probe DB for the most recent insert timestamp → pick adaptive threshold.
      2. Fetch full active inventory for deduplication.
      3. Set purchase_date = datetime.now() (Python owns this — no LLM dates).
      4. For each candidate row, fuzzy-match against DB using adaptive threshold:
           • Condition A (same-day match): duplicate receipt scan → skip.
           • Condition B (older match):    restock of existing item → retire old row.
      5. Batch-insert new rows; batch-mark old rows as 'consumed'.
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting smart database synchronization...")

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("ERROR: Supabase credentials missing in environment.")
        return

    # Python owns the purchase date — zero LLM date hallucination risk
    purchase_date     = datetime.now()
    purchase_date_str = purchase_date.strftime("%Y-%m-%d")

    try:
        # Step 1: Determine adaptive threshold from recent-scan probe
        latest_ts = get_latest_item_timestamp(supabase_url, supabase_key)
        threshold = detect_scan_mode(latest_ts)

        # Step 2: Fetch full active inventory
        active_items = get_active_items(supabase_url, supabase_key)
        active_dict  = {item["item_name"]: item for item in active_items}
    except Exception as e:
        print(f"DATABASE ERROR (Fetching): {e}")
        return

    candidate_rows, skipped   = build_fridge_rows(llm_payload.get("items", []), purchase_date)
    items_to_insert            = []
    old_items_to_mark_consumed = []
    skipped_duplicates         = 0

    for row in candidate_rows:
        matched_old = find_best_match(row["item_name"], active_dict, threshold=threshold)

        if matched_old:
            if matched_old["purchase_date"] == purchase_date_str:
                # Condition A: same-day match → duplicate scan, skip silently
                skipped_duplicates += 1
                continue
            elif purchase_date_str > matched_old["purchase_date"]:
                # Condition B: restock → retire old entry
                old_items_to_mark_consumed.append(matched_old["id"])

        items_to_insert.append(row)

    try:
        if old_items_to_mark_consumed:
            update_consumed_items(supabase_url, supabase_key, old_items_to_mark_consumed)
            print(f"UPDATE: Marked {len(old_items_to_mark_consumed)} old item(s) as 'consumed'.")

        if items_to_insert:
            endpoint = f"{supabase_url}/rest/v1/fridge_items"
            headers  = _build_headers(supabase_key, {
                "Content-Type": "application/json",
                "Prefer":       "return=minimal",
            })
            response = requests.post(endpoint, json=items_to_insert, headers=headers)
            response.raise_for_status()
            print(f"SUCCESS: {len(items_to_insert)} new item(s) saved to your virtual fridge.")

        if skipped_duplicates:
            print(f"INFO: Skipped {skipped_duplicates} item(s) — duplicate scan (threshold: {threshold}).")

        if skipped:
            print(f"INFO: Skipped {len(skipped)} item(s) with no valid expiry estimate: {skipped}")

    except Exception as e:
        print(f"DATABASE ERROR (Saving/Updating): {e}")


def run_scanner(image_path: str) -> None:
    """
    Cloud Function entry point.
    Ingests a receipt image and persists parsed food items into the fridge DB.
    """
    try:
        llm_payload = analyze_receipt(image_path)
        save_to_db(llm_payload)
    except FileNotFoundError:
        print(f"ERROR: Image file '{image_path}' not found.")
    except ValueError as e:
        print(f"PARSE ERROR: {e}")
    except Exception as e:
        print(f"GENERAL ERROR: {e}")


if __name__ == "__main__":
    run_scanner("receipt1.jpg")
