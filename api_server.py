"""
api_server.py  —  Smart Fridge REST API Server

Exposes the chef_agent.py core logic over HTTP for the Smart Fridge Flutter app.
chef_agent.py is NOT modified — all reusable functions are imported directly.

Architecture
────────────
  • FastAPI handles routing, request validation (Pydantic), and CORS.
  • An in-memory session store (dict) maps user_id → { chat, active_items, recipe }.
      - Created on  POST /generate_recipe
      - Updated on  POST /revise_recipe
      - Consumed on POST /confirm_recipe  (session destroyed after deduction)
  • The Gemini chat object is stored per-session so revision requests can build
    on conversation history without re-sending the full fridge inventory.

⚠  Production notes
  - Replace _sessions dict with Redis for multi-process / multi-pod deployments.
  - Restrict CORS allow_origins to your Flutter app's actual origin before launch.
  - Add authentication middleware (JWT / API key) on all endpoints.
  - Consider wrapping Supabase calls with asyncio.to_thread() for full async I/O.

Installation
────────────
  pip install fastapi uvicorn

Run (development — auto-reload on file changes)
───────────────────────────────────────────────
  uvicorn api_server:app --reload --port 8000

Endpoints
─────────
  GET  /health           — liveness check
  GET  /fridge_items     — full active inventory (for the fridge overview screen)
  POST /generate_recipe  — generate + optionally scale a recipe from a vibe prompt
  POST /revise_recipe    — send freeform feedback; get an updated recipe
  POST /confirm_recipe   — deduct used items from DB; add depleted ones to shopping list
"""

import difflib
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ── Import reusable logic from chef_agent — no interactive loop is triggered ──
# run_chef_agent() is guarded by  `if __name__ == "__main__":` in chef_agent.py,
# so importing the module is safe. load_dotenv() and genai.Client() run once at
# import time, which is the desired behaviour for a long-lived server process.
from chef_agent import (
    _build_initial_prompt,
    _build_revision_prompt,
    _create_chef_chat,
    _patch_fridge_item,
    _send_and_parse,
    add_to_smart_list,
    get_all_active_items,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# App + CORS
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Smart Fridge API",
    description="Personal Chef AI backend for the Smart Fridge Flutter app.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # ← restrict to your Flutter origin in production
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────────
# In-memory session store
#
# Maps user_id (str) → {
#     "chat":         Gemini chat object  (retains conversation history),
#     "active_items": list[dict]          (fridge snapshot at generation time),
#     "recipe":       dict                (latest generated / revised recipe),
#     "created_at":   datetime,
# }
#
# Production: replace with Redis using pickle or JSON serialisation for the
# chat object, or re-create chat from stored history on each request.
# ──────────────────────────────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic request / response models
# ──────────────────────────────────────────────────────────────────────────────

class GenerateRecipeRequest(BaseModel):
    user_id: str = Field(..., description="Unique user identifier (UUID)")
    prompt:  str = Field(..., description="Culinary vibe, e.g. 'ארוחת בוקר קלילה'")
    guests:  int = Field(default=1, ge=1, le=20, description="Number of diners to scale for")


class GenerateRecipeResponse(BaseModel):
    recipe:       dict
    active_items: list[dict]
    guests:       int


class ReviseRecipeRequest(BaseModel):
    user_id:  str = Field(..., description="Unique user identifier (UUID)")
    feedback: str = Field(..., description="Freeform change request, e.g. 'תעשה את זה טבעוני'")


class ConfirmRecipeRequest(BaseModel):
    user_id: str = Field(..., description="Unique user identifier (UUID)")


class DeductedItem(BaseModel):
    item_name:      str
    quantity_before: float
    quantity_deducted: float
    quantity_after:  float
    fully_consumed: bool


class ConfirmRecipeResponse(BaseModel):
    status:                  str
    deducted_items:          list[DeductedItem]
    shopping_list_additions: list[str]


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _build_scaling_prompt(guests: int) -> str:
    """Prompt that asks the LLM to scale the current recipe to `guests` people."""
    return (
        f"הלקוח אישר את המתכון. אנא עדכן את כל הכמויות במתכון עבור {guests} סועדים. "
        "ודא שסך הכמויות לא חורג מהמלאי הזמין. "
        "החזר את ה-JSON המלא והמעודכן."
    )


def _require_session(user_id: str) -> dict:
    """Returns the session for user_id, or raises 404 if it doesn't exist."""
    session = _sessions.get(user_id)
    if not session:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No active session for user_id='{user_id}'. "
                "Call POST /generate_recipe first."
            ),
        )
    return session


def _resolve_fridge_item(name: str, fridge_by_name: dict) -> dict | None:
    """
    Exact-match lookup first; falls back to difflib fuzzy match at 70% similarity.
    Returns the matched fridge item dict, or None if no match is found.
    """
    db_item = fridge_by_name.get(name)
    if db_item:
        return db_item

    close = difflib.get_close_matches(name, list(fridge_by_name.keys()), n=1, cutoff=0.70)
    if close:
        log.info("Fuzzy match: '%s' → '%s'", name, close[0])
        return fridge_by_name[close[0]]

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Meta"])
def health() -> dict:
    """Liveness check — returns server status and current timestamp."""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/fridge_items", tags=["Inventory"])
def fridge_items() -> list[dict]:
    """
    Returns the full active fridge inventory sorted by soonest expiry.
    Use this endpoint to populate the fridge overview screen in the Flutter app.
    Non-food items (deposits, bags, 'אחר' category) are filtered out before returning.
    """
    items = get_all_active_items()
    return items


@app.post("/generate_recipe", response_model=GenerateRecipeResponse, tags=["Chef"])
def generate_recipe(body: GenerateRecipeRequest) -> GenerateRecipeResponse:
    """
    Generates a recipe tailored to the user's fridge inventory and requested vibe.

    Flow:
      1. Fetch the full active inventory from Supabase.
      2. Open a new stateful Gemini chat session (SYSTEM_INSTRUCTION loaded once).
      3. Generate a base recipe for 1 person (PORTION CONTROL rule in system prompt).
      4. If guests > 1: send a scaling follow-up to the same chat session.
      5. Store { chat, active_items, recipe } in the session store keyed by user_id.
      6. Return the recipe dict and the active inventory for the Flutter app to display.

    Calling this endpoint again for the same user_id replaces the existing session.
    """
    log.info(
        "generate_recipe  user=%s  prompt=%r  guests=%d",
        body.user_id, body.prompt, body.guests,
    )

    # Step 1 — fetch inventory
    active_items = get_all_active_items()
    if not active_items:
        raise HTTPException(
            status_code=409,
            detail="המקרר ריק — אין פריטים פעילים במלאי.",
        )

    # Step 2 — open a stateful Gemini chat session
    chat = _create_chef_chat()

    # Step 3 — generate base recipe (1 person, per PORTION CONTROL system rule)
    try:
        recipe = _send_and_parse(chat, _build_initial_prompt(active_items, body.prompt))
    except Exception as e:
        log.error("LLM error during initial generation: %s", e)
        raise HTTPException(status_code=502, detail=f"שגיאת AI: {e}")

    if recipe.get("_raw_fallback"):
        raise HTTPException(
            status_code=502,
            detail="ה-AI לא הצליח להחזיר מתכון מסודר. נסה לנסח את הבקשה מחדש.",
        )

    # Step 4 — scale to requested guest count (non-fatal if it fails)
    if body.guests > 1:
        try:
            scaled = _send_and_parse(chat, _build_scaling_prompt(body.guests))
            if not scaled.get("_raw_fallback"):
                recipe = scaled
                log.info("Recipe scaled to %d guests for user=%s", body.guests, body.user_id)
            else:
                log.warning("Scaling returned raw fallback; keeping 1-person recipe.")
        except Exception as e:
            log.warning("Scaling failed (%s); returning 1-person recipe.", e)

    # Step 5 — persist session
    _sessions[body.user_id] = {
        "chat":         chat,
        "active_items": active_items,
        "recipe":       recipe,
        "created_at":   datetime.now(),
    }
    log.info("Session stored  user=%s  recipe=%r", body.user_id, recipe.get("recipe_name"))

    return GenerateRecipeResponse(
        recipe=recipe,
        active_items=active_items,
        guests=body.guests,
    )


@app.post("/revise_recipe", response_model=GenerateRecipeResponse, tags=["Chef"])
def revise_recipe(body: ReviseRecipeRequest) -> GenerateRecipeResponse:
    """
    Sends freeform user feedback to the existing Gemini chat session and returns
    a revised recipe.

    Because the session retains full conversation history, the LLM builds on the
    previous recipe without the inventory needing to be re-sent. This mirrors the
    CLI refinement loop in chef_agent.run_chef_agent().

    Requires an active session (call /generate_recipe first).
    """
    log.info("revise_recipe  user=%s  feedback=%r", body.user_id, body.feedback)

    session = _require_session(body.user_id)

    try:
        revised = _send_and_parse(session["chat"], _build_revision_prompt(body.feedback))
    except Exception as e:
        log.error("LLM error during revision: %s", e)
        raise HTTPException(status_code=502, detail=f"שגיאת AI: {e}")

    if revised.get("_raw_fallback"):
        raise HTTPException(
            status_code=502,
            detail="ה-AI לא הצליח לעדכן את המתכון. נסה לנסח את הבקשה אחרת.",
        )

    # Update the stored recipe so /confirm_recipe uses the revised version
    session["recipe"] = revised
    _sessions[body.user_id] = session
    log.info("Session updated (revised)  user=%s", body.user_id)

    return GenerateRecipeResponse(
        recipe=revised,
        active_items=session["active_items"],
        guests=1,
    )


@app.post("/confirm_recipe", response_model=ConfirmRecipeResponse, tags=["Chef"])
def confirm_recipe(body: ConfirmRecipeRequest) -> ConfirmRecipeResponse:
    """
    Executes the inventory deduction for the confirmed recipe and destroys the session.

    For each item in recipe.used_fridge_items:
      • Deducts quantity_used (float) from the current DB quantity.
      • remaining <= 0  →  status='consumed', quantity=0, item added to smart_shopping_list.
      • remaining > 0   →  quantity field updated with the precise remaining float value.

    Uses the active_items snapshot captured at /generate_recipe time as the source of
    truth for item IDs and current quantities (avoids an extra DB round-trip).
    Fuzzy matching (70% threshold) handles minor LLM name drift.

    Returns a structured summary of every deduction for the Flutter app to display.
    """
    log.info("confirm_recipe  user=%s", body.user_id)

    session      = _require_session(body.user_id)
    recipe       = session["recipe"]
    active_items = session["active_items"]

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=500, detail="Supabase credentials missing from environment.")

    used_items = recipe.get("used_fridge_items", [])
    if not used_items:
        raise HTTPException(
            status_code=422,
            detail="המתכון אינו מכיל פריטים לניכוי מהמלאי.",
        )

    fridge_by_name     = {item["item_name"]: item for item in active_items}
    deducted:   list[DeductedItem] = []
    shopping:   list[str]          = []

    for used in used_items:
        name     = used.get("item_name", "").strip()
        qty_used = max(1.0, float(used.get("quantity_used", 1.0)))

        db_item = _resolve_fridge_item(name, fridge_by_name)
        if not db_item:
            log.warning("No inventory match for '%s' — skipping.", name)
            continue

        item_id       = db_item["id"]
        current_qty   = float(db_item.get("quantity", 1.0))
        remaining_qty = round(current_qty - qty_used, 3)

        try:
            if remaining_qty <= 0:
                _patch_fridge_item(supabase_url, supabase_key, item_id, {
                    "status":   "consumed",
                    "quantity": 0,
                })
                add_to_smart_list(supabase_url, supabase_key, db_item["item_name"])
                shopping.append(db_item["item_name"])
                deducted.append(DeductedItem(
                    item_name=db_item["item_name"],
                    quantity_before=current_qty,
                    quantity_deducted=qty_used,
                    quantity_after=0.0,
                    fully_consumed=True,
                ))
            else:
                _patch_fridge_item(supabase_url, supabase_key, item_id, {
                    "quantity": remaining_qty,
                })
                deducted.append(DeductedItem(
                    item_name=db_item["item_name"],
                    quantity_before=current_qty,
                    quantity_deducted=qty_used,
                    quantity_after=remaining_qty,
                    fully_consumed=False,
                ))

            log.info(
                "Deducted '%s': %.3f → %.3f",
                db_item["item_name"], current_qty, max(0.0, remaining_qty),
            )
        except Exception as e:
            # Non-fatal: log and continue so other items are still processed
            log.error("DB error updating '%s': %s", db_item["item_name"], e)

    # Destroy the session — the conversation is complete
    _sessions.pop(body.user_id, None)
    log.info(
        "Session destroyed  user=%s  deducted=%d  shopping_list=%d",
        body.user_id, len(deducted), len(shopping),
    )

    return ConfirmRecipeResponse(
        status="success",
        deducted_items=deducted,
        shopping_list_additions=shopping,
    )
