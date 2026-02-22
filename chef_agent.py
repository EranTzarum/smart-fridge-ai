# import os
# import requests
# from datetime import datetime, timedelta
# from dotenv import load_dotenv
# from google import genai

# # Load environment variables
# load_dotenv()
# client = genai.Client()

# def get_expiring_items(days_ahead=7):
#     print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching inventory from Supabase DB...")
#     supabase_url = os.environ.get("SUPABASE_URL")
#     supabase_key = os.environ.get("SUPABASE_KEY")
    
#     target_date = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    
#     # Query: Select active items expiring on or before the target date
#     endpoint = f"{supabase_url}/rest/v1/fridge_items?select=item_name,quantity,expiry_date&status=eq.active&expiry_date=lte.{target_date}"
    
#     headers = {
#         "apikey": supabase_key,
#         "Authorization": f"Bearer {supabase_key}"
#     }
    
#     response = requests.get(endpoint, headers=headers)
#     response.raise_for_status()
#     return response.json()

# def generate_recipe(items):
#     if not items:
#         print("SUCCESS: No expiring items found in the near future. Your fridge is optimized.")
#         return
        
#     print(f"[{datetime.now().strftime('%H:%M:%S')}] Found {len(items)} expiring items. Generating AI recipe...")
    
#     ingredients_list = "\n".join([f"- {item['item_name']} (Quantity: {item['quantity']}, Expires: {item['expiry_date']})" for item in items])
    
#     prompt = f"""
#     You are the Smart-Fridge Chef. Your ultimate goal is zero food waste.
#     Here are the items in the user's fridge that are expiring soon and MUST be used:
    
#     {ingredients_list}
    
#     Create ONE practical, delicious recipe using mostly these ingredients to prevent them from being thrown away.
#     Assume the user has basic pantry staples (salt, pepper, oil).
    
#     Output in Hebrew. Keep it sharp, direct, and structured with:
#     1. שם המתכון (Recipe Name)
#     2. מצרכים נדרשים (Ingredients - highlight the expiring ones you are saving)
#     3. הוראות הכנה (Brief Instructions)
#     """
    
#     response = client.models.generate_content(
#         model='gemini-2.5-flash',
#         contents=prompt
#     )
#     print("\n=== THE SMART CHEF SUGGESTS ===")
#     print(response.text)

# if __name__ == "__main__":
#     try:
#         expiring_items = get_expiring_items()
#         generate_recipe(expiring_items)
#     except Exception as e:
#         print(f"GENERAL ERROR: {e}")


import os
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google import genai

# Load environment variables
load_dotenv()
client = genai.Client()

def get_urgent_items(days_ahead=14):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning virtual fridge for items expiring soon...")
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        print("ERROR: Supabase credentials missing.")
        return []

    target_date = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    
    # Query: Active items expiring soon. We ignore frozen items (which usually have 90+ days expiry)
    endpoint = f"{supabase_url}/rest/v1/fridge_items?select=item_name,quantity,expiry_date&status=eq.active&expiry_date=lte.{target_date}"
    
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}"
    }
    
    try:
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()
        items = response.json()
        
        # Sort by expiry date (most urgent first)
        items.sort(key=lambda x: x['expiry_date'])
        return items
    except Exception as e:
        print(f"DATABASE ERROR: {e}")
        return []

def chat_with_chef():
    urgent_items = get_urgent_items()
    
    if not urgent_items:
        print("\n[CHEF]: Your fridge is completely optimized. Nothing is expiring in the next 14 days.")
        return

    print("\n==================================================")
    print("👨‍🍳 SMART CHEF IS ONLINE")
    print("==================================================")
    print(f"[CHEF]: I see {len(urgent_items)} items in your fridge that need to be used soon.")
    print("Here are the top priorities:")
    
    # Show top 5 most urgent items to the user
    ingredients_text = ""
    for item in urgent_items[:7]:
        print(f" - {item['item_name']} (Expires: {item['expiry_date']})")
        ingredients_text += f"- {item['item_name']} (Qty: {item['quantity']})\n"
    
    print("\n[CHEF]: What's your vibe today? (e.g., Italian, something baked, light salad, quick dinner)")
    user_preference = input(">> Your preference: ")
    
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Chef is designing a '{user_preference}' recipe to save your food...")
    
    prompt = f"""
    You are the 'Smart-Fridge Chef'. Your goal is to achieve ZERO food waste by creating recipes from expiring ingredients.
    
    Here are the user's urgent items that MUST be used:
    {ingredients_text}
    
    The user specifically requested this style/vibe: "{user_preference}"
    
    Assume the user has basic pantry staples (oil, salt, pepper, basic spices, garlic).
    Design a practical, delicious recipe in Hebrew that matches their request AND uses as many of the urgent items as possible.
    
    Structure:
    1. שם המתכון (Recipe Name)
    2. למה בחרתי בו (Why this fits the user's request and saves their food)
    3. מצרכים (Ingredients - separate urgent fridge items vs pantry staples)
    4. הכנה (Short, clear steps)
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        print("\n" + response.text)
    except Exception as e:
        print(f"AI ERROR: {e}")

if __name__ == "__main__":
    chat_with_chef()