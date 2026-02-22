# import os
# import json
# import time
# import requests
# from datetime import datetime, timedelta
# from dotenv import load_dotenv
# from google import genai
# import PIL.Image

# # Load environment variables
# load_dotenv()

# # Initialize Gemini Client
# client = genai.Client()

# def analyze_receipt(image_path):
#     print(f"[{datetime.now().strftime('%H:%M:%S')}] Sending receipt to Gemini for analysis...")
#     img = PIL.Image.open(image_path)
#     current_date = datetime.now().strftime("%Y-%m-%d")
    
#     prompt = f"""
#     You are the core AI engine for 'Smart-Fridge'. Analyze the attached grocery receipt.
#     Today's date is {current_date}.
    
#     CRITICAL INSTRUCTIONS:
#     1. DATE: Extract the receipt date. If not found, use today's date.
#     2. AGGREGATE: If the exact same item appears multiple times, combine them into a SINGLE JSON object and SUM the quantities.
#     3. CATEGORIZE: Assign each item to exactly ONE of these categories: "מוצרי חלב וביצים", "בשר ודגים", "פירות וירקות", "מזווה", "נשנושים ומתוקים", "משקאות", "אחר".
#     4. STORAGE & EXPIRY PHYSICS (CRITICAL):
#        - If the item is fresh meat, poultry, or fish, assume the user will FREEZE it. Set expiry to 90-120 days.
#        - If the item is dry pantry goods (sugar, pasta, cans), set expiry to 365 days.
#        - If the item is fresh dairy (milk, cottage cheese) or fresh vegetables, assume FRIDGE and set short expiry (5-14 days).
    
#     Return ONLY a valid JSON object matching this exact structure:
#     {{
#         "receipt_date": "YYYY-MM-DD",
#         "items": [
#             {{
#                 "item_name": "string (translated to Hebrew)",
#                 "category": "string (from the list above)",
#                 "quantity": number (summed if duplicate),
#                 "estimated_expiry_days": number
#             }}
#         ]
#     }}
    
#     Do not include any markdown tags like ```json. Return ONLY the raw JSON object.
#     """
    
#     max_retries = 3
#     for attempt in range(max_retries):
#         try:
#             response = client.models.generate_content(
#                 model='gemini-2.5-flash',
#                 contents=[prompt, img]
#             )
#             return response.text
#         except Exception as e:
#             if "503" in str(e) or "UNAVAILABLE" in str(e):
#                 if attempt < max_retries - 1:
#                     wait_time = 2 ** attempt # Wait 1s, then 2s
#                     print(f"WARNING: Google API overloaded (503). Retrying in {wait_time}s (Attempt {attempt + 1}/{max_retries})...")
#                     time.sleep(wait_time)
#                 else:
#                     raise e
#             else:
#                 raise e

# def get_active_items(supabase_url, supabase_key):
#     """Fetch only items currently active in the fridge."""
#     endpoint = f"{supabase_url}/rest/v1/fridge_items?status=eq.active&select=id,item_name,purchase_date"
#     headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
#     response = requests.get(endpoint, headers=headers)
#     response.raise_for_status()
#     return response.json()

# def update_consumed_items(supabase_url, supabase_key, item_ids):
#     """Mark older items as consumed when a new restock is detected."""
#     if not item_ids: return
#     ids_string = ",".join(item_ids)
#     endpoint = f"{supabase_url}/rest/v1/fridge_items?id=in.({ids_string})"
#     headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}", "Content-Type": "application/json"}
#     response = requests.patch(endpoint, json={"status": "consumed"}, headers=headers)
#     response.raise_for_status()

# def save_to_db(parsed_data):
#     print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting smart database synchronization...")
    
#     supabase_url = os.environ.get("SUPABASE_URL")
#     supabase_key = os.environ.get("SUPABASE_KEY")
#     if not supabase_url or not supabase_key:
#         print("ERROR: Supabase credentials missing in .env file.")
#         return

#     # 1. Fetch current active fridge inventory
#     try:
#         active_items = get_active_items(supabase_url, supabase_key)
#         active_dict = {item['item_name']: item for item in active_items}
#     except Exception as e:
#         print(f"DATABASE ERROR (Fetching): {e}")
#         return

#     receipt_date_str = parsed_data.get("receipt_date")
#     receipt_date = datetime.strptime(receipt_date_str, "%Y-%m-%d")
    
#     items_to_insert = []
#     old_items_to_mark_consumed = []
#     skipped_duplicates = 0
    
#     # 2. Compare new receipt against active inventory
#     for item in parsed_data.get("items", []):
#         name = item["item_name"]
        
#         # Smart Logic: Check if item already exists in the fridge
#         if name in active_dict:
#             old_item = active_dict[name]
#             # Condition A: Accidental double scan of the SAME receipt (same date)
#             if old_item["purchase_date"] == receipt_date_str:
#                 skipped_duplicates += 1
#                 continue # Skip inserting this item
            
#             # Condition B: New shopping trip, user bought an item they already had
#             elif receipt_date_str > old_item["purchase_date"]:
#                 old_items_to_mark_consumed.append(old_item["id"])
        
#         # Calculate expiry
#         expiry_days = item.get("estimated_expiry_days", 0)
#         expiry_date = receipt_date + timedelta(days=expiry_days)
        
#         db_row = {
#             "item_name": name,
#             "category": item["category"],
#             "quantity": item["quantity"],
#             "purchase_date": receipt_date_str,
#             "expiry_date": expiry_date.strftime("%Y-%m-%d"),
#             "status": "active"
#         }
#         items_to_insert.append(db_row)
    
#     # 3. Execute DB updates
#     try:
#         # Update old items to 'consumed'
#         if old_items_to_mark_consumed:
#             update_consumed_items(supabase_url, supabase_key, old_items_to_mark_consumed)
#             print(f"UPDATE: Marked {len(old_items_to_mark_consumed)} old items as 'consumed'.")
        
#         # Insert new items
#         if items_to_insert:
#             endpoint = f"{supabase_url}/rest/v1/fridge_items"
#             headers = {
#                 "apikey": supabase_key,
#                 "Authorization": f"Bearer {supabase_key}",
#                 "Content-Type": "application/json",
#                 "Prefer": "return=minimal"
#             }
#             response = requests.post(endpoint, json=items_to_insert, headers=headers)
#             response.raise_for_status()
#             print(f"SUCCESS: {len(items_to_insert)} new items saved to your virtual fridge.")
        
#         if skipped_duplicates > 0:
#             print(f"INFO: Skipped {skipped_duplicates} items (Duplicate scan detected).")
            
#     except Exception as e:
#         print(f"DATABASE ERROR (Saving/Updating): {e}")

# if __name__ == "__main__":
#     receipt_image = "receipt1.jpg" 
#     try:
#         result = analyze_receipt(receipt_image)
#         clean_result = result.replace('```json', '').replace('```', '').strip()
#         parsed_json = json.loads(clean_result)
#         save_to_db(parsed_json)
#     except FileNotFoundError:
#         print(f"ERROR: Image file '{receipt_image}' not found.")
#     except json.JSONDecodeError:
#         print("ERROR: Model failed to return valid JSON. Raw output:")
#         print(result)
#     except Exception as e:
#         print(f"GENERAL ERROR: {e}")


import os
import json
import time
import difflib
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google import genai
import PIL.Image

# Load environment variables
load_dotenv()

# Initialize Gemini Client
client = genai.Client()

def analyze_receipt(image_path):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Sending receipt to Gemini for analysis with Master Catalog cleaning...")
    img = PIL.Image.open(image_path)
    current_date = datetime.now().strftime("%Y-%m-%d")
    
    prompt = f"""
    You are the core AI engine for 'Smart-Fridge'. Analyze the attached grocery receipt.
    Today's date is {current_date}.
    
    CRITICAL INSTRUCTIONS:
    1. DATE: Extract the receipt date. If not found, use today's date.
    2. AGGREGATE: Combine identical items into a SINGLE JSON object and SUM the quantities.
    
    3. MASTER CATALOG MAPPING (CRITICAL):
       Map the recognized item name to the closest generic product name in Hebrew. 
       Ignore brand names and specific weights in the final 'item_name'. 
       For example: "קרם גבינה 500 ג 5%" -> "קרם גבינה 5%", "מלבפון ישראל" -> "מלפפון", "תפוז אפר לבן" -> "תפוח אדמה".
    
    4. CATEGORIZE: Assign each item to exactly ONE of these categories: "מוצרי חלב וביצים", "בשר ודגים", "פירות וירקות", "מזווה", "נשנושים ומתוקים", "משקאות", "אחר".
    
    5. STORAGE & EXPIRY PHYSICS (CRITICAL):
       - If the item is fresh meat, poultry, or fish, assume the user will FREEZE it. Set expiry to 90-120 days.
       - If the item is dry pantry goods (sugar, pasta, cans), set expiry to 365 days.
       - If the item is fresh dairy (milk, cottage cheese) or fresh vegetables, assume FRIDGE and set short expiry (5-14 days).
    
    Return ONLY a valid JSON object matching this exact structure:
    {{
        "receipt_date": "YYYY-MM-DD",
        "items": [
            {{
                "item_name": "string (cleaned and standardized in Hebrew)",
                "category": "string (from the list above)",
                "quantity": number (summed if duplicate),
                "estimated_expiry_days": number
            }}
        ]
    }}
    
    Do not include any markdown tags like ```json. Return ONLY the raw JSON object.
    """
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, img]
            )
            return response.text
        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"WARNING: Google API overloaded. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    raise e
            else:
                raise e

def get_active_items(supabase_url, supabase_key):
    """Fetch only items currently active in the fridge."""
    endpoint = f"{supabase_url}/rest/v1/fridge_items?status=eq.active&select=id,item_name,purchase_date"
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
    response = requests.get(endpoint, headers=headers)
    response.raise_for_status()
    return response.json()

def update_consumed_items(supabase_url, supabase_key, item_ids):
    """Mark older items as consumed."""
    if not item_ids: return
    ids_string = ",".join(item_ids)
    endpoint = f"{supabase_url}/rest/v1/fridge_items?id=in.({ids_string})"
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}", "Content-Type": "application/json"}
    response = requests.patch(endpoint, json={"status": "consumed"}, headers=headers)
    response.raise_for_status()

def find_best_match(target_name, active_items_dict, threshold=0.80):
    """
    Fuzzy Matching: Finds the closest matching item name in the database.
    Returns the matching DB item if similarity is above the threshold (80%).
    """
    existing_names = list(active_items_dict.keys())
    # get_close_matches returns a list of matches, highest similarity first
    matches = difflib.get_close_matches(target_name, existing_names, n=1, cutoff=threshold)
    
    if matches:
        return active_items_dict[matches[0]]
    return None

def save_to_db(parsed_data):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting smart database synchronization...")
    
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("ERROR: Supabase credentials missing.")
        return

    try:
        active_items = get_active_items(supabase_url, supabase_key)
        active_dict = {item['item_name']: item for item in active_items}
    except Exception as e:
        print(f"DATABASE ERROR (Fetching): {e}")
        return

    receipt_date_str = parsed_data.get("receipt_date")
    receipt_date = datetime.strptime(receipt_date_str, "%Y-%m-%d")
    
    items_to_insert = []
    old_items_to_mark_consumed = []
    skipped_duplicates = 0
    
    for item in parsed_data.get("items", []):
        name = item["item_name"]
        
        # FUZZY MATCHING LOGIC INSTEAD OF EXACT MATCH
        matched_old_item = find_best_match(name, active_dict, threshold=0.80)
        
        if matched_old_item:
            # Condition A: Accidental double scan of the SAME receipt
            if matched_old_item["purchase_date"] == receipt_date_str:
                skipped_duplicates += 1
                continue 
            
            # Condition B: New restock of an existing item
            elif receipt_date_str > matched_old_item["purchase_date"]:
                old_items_to_mark_consumed.append(matched_old_item["id"])
        
        expiry_days = item.get("estimated_expiry_days", 0)
        expiry_date = receipt_date + timedelta(days=expiry_days)
        
        db_row = {
            "item_name": name,
            "category": item["category"],
            "quantity": item["quantity"],
            "purchase_date": receipt_date_str,
            "expiry_date": expiry_date.strftime("%Y-%m-%d"),
            "status": "active"
        }
        items_to_insert.append(db_row)
    
    try:
        if old_items_to_mark_consumed:
            update_consumed_items(supabase_url, supabase_key, old_items_to_mark_consumed)
            print(f"UPDATE: Marked {len(old_items_to_mark_consumed)} old items as 'consumed'.")
        
        if items_to_insert:
            endpoint = f"{supabase_url}/rest/v1/fridge_items"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            }
            response = requests.post(endpoint, json=items_to_insert, headers=headers)
            response.raise_for_status()
            print(f"SUCCESS: {len(items_to_insert)} new items saved to your virtual fridge.")
        
        if skipped_duplicates > 0:
            print(f"INFO: Skipped {skipped_duplicates} items (Duplicate scan or fuzzy match detected).")
            
    except Exception as e:
        print(f"DATABASE ERROR: {e}")

if __name__ == "__main__":
    receipt_image = "receipt1.jpg" 
    try:
        result = analyze_receipt(receipt_image)
        clean_result = result.replace('```json', '').replace('```', '').strip()
        parsed_json = json.loads(clean_result)
        save_to_db(parsed_json)
    except Exception as e:
        print(f"GENERAL ERROR: {e}")