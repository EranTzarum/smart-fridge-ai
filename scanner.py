# import os
# from dotenv import load_dotenv
# from google import genai
# import PIL.Image
# import json
# from datetime import datetime

# load_dotenv()
# client = genai.Client()

# def analyze_receipt(image_path):
#     print(f"Starting advanced analysis of receipt: {image_path}...")
#     img = PIL.Image.open(image_path)
    
#     current_date = datetime.now().strftime("%Y-%m-%d")
    
#     prompt = f"""
#     You are the core AI engine for 'Smart-Fridge'. Analyze the attached grocery receipt.
#     Today's date is {current_date}.
    
#     CRITICAL INSTRUCTIONS:
#     1. DATE: Extract the receipt date. If not found, use today's date.
#     2. AGGREGATE: If the exact same item appears multiple times, combine them into a SINGLE JSON object and SUM the quantities.
#     3. CATEGORIZE: Assign each item to exactly ONE of these categories: "מוצרי חלב וביצים", "בשר ודגים", "פירות וירקות", "מזווה", "נשנושים ומתוקים", "משקאות", "אחר".
#     4. EXPIRY: Estimate expiration in days from the receipt date.
    
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
    
#     response = client.models.generate_content(
#         model='gemini-2.5-flash',
#         contents=[prompt, img]
#     )
#     return response.text

# if __name__ == "__main__":
#     receipt_image = "receipt1.jpg" 
    
#     try:
#         result = analyze_receipt(receipt_image)
#         print("--- Structured Data Ready for DB ---")
        
#         clean_result = result.replace('```json', '').replace('```', '').strip()
#         parsed_json = json.loads(clean_result)
        
#         print(json.dumps(parsed_json, indent=4, ensure_ascii=False))
        
#     except FileNotFoundError:
#         print(f"Error: Could not find '{receipt_image}'.")
#     except Exception as e:
#         print(f"Error during analysis: {e}")
#         if 'result' in locals():
#             print("Raw response:")
#             print(result)

import os
import json
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Sending receipt to Gemini for analysis...")
    img = PIL.Image.open(image_path)
    
    current_date = datetime.now().strftime("%Y-%m-%d")
    
    prompt = f"""
    You are the core AI engine for 'Smart-Fridge'. Analyze the attached grocery receipt.
    Today's date is {current_date}.
    
    CRITICAL INSTRUCTIONS:
    1. DATE: Extract the receipt date. If not found, use today's date.
    2. AGGREGATE: If the exact same item appears multiple times, combine them into a SINGLE JSON object and SUM the quantities.
    3. CATEGORIZE: Assign each item to exactly ONE of these categories: "מוצרי חלב וביצים", "בשר ודגים", "פירות וירקות", "מזווה", "נשנושים ומתוקים", "משקאות", "אחר".
    4. EXPIRY: Estimate expiration in days from the receipt date.
    
    Return ONLY a valid JSON object matching this exact structure:
    {{
        "receipt_date": "YYYY-MM-DD",
        "items": [
            {{
                "item_name": "string (translated to Hebrew)",
                "category": "string (from the list above)",
                "quantity": number (summed if duplicate),
                "estimated_expiry_days": number
            }}
        ]
    }}
    
    Do not include any markdown tags like ```json. Return ONLY the raw JSON object.
    """
    
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[prompt, img]
    )
    return response.text

def save_to_db(parsed_data):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting database synchronization...")
    
    receipt_date_str = parsed_data.get("receipt_date")
    receipt_date = datetime.strptime(receipt_date_str, "%Y-%m-%d")
    
    items_to_insert = []
    
    # Map parsed data to database schema
    for item in parsed_data.get("items", []):
        expiry_days = item.get("estimated_expiry_days", 0)
        # Calculate precise expiration date
        expiry_date = receipt_date + timedelta(days=expiry_days)
        
        db_row = {
            "item_name": item["item_name"],
            "category": item["category"],
            "quantity": item["quantity"],
            "purchase_date": receipt_date_str,
            "expiry_date": expiry_date.strftime("%Y-%m-%d"),
            "status": "active"
        }
        items_to_insert.append(db_row)
    
    # Push data directly to Supabase REST API
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        print("ERROR: Supabase credentials missing in .env file.")
        return

    endpoint = f"{supabase_url}/rest/v1/fridge_items"
    
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal" # Do not return the inserted rows to save bandwidth
    }
    
    try:
        response = requests.post(endpoint, json=items_to_insert, headers=headers)
        response.raise_for_status() # Trigger exception if status code is 4xx/5xx
        print(f"SUCCESS: {len(items_to_insert)} items saved to your virtual fridge.")
    except Exception as e:
        print(f"DATABASE ERROR: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response details: {e.response.text}")

if __name__ == "__main__":
    receipt_image = "receipt1.jpg" 
    
    try:
        # Step 1: Analysis
        result = analyze_receipt(receipt_image)
        clean_result = result.replace('```json', '').replace('```', '').strip()
        parsed_json = json.loads(clean_result)
        
        # Step 2: Save to DB
        save_to_db(parsed_json)
        
    except FileNotFoundError:
        print(f"ERROR: Image file '{receipt_image}' not found.")
    except json.JSONDecodeError:
        print("ERROR: Model failed to return valid JSON. Raw output:")
        print(result)
    except Exception as e:
        print(f"GENERAL ERROR: {e}")