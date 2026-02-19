# Smart-Fridge AI 🧊🧠

An AI-native smart fridge application designed to eliminate food waste through intelligent inventory management and predictive recipe generation.

## Core Features (PoC Phase)
* **Semantic Receipt Scanner:** Uses Google's Gemini 2.5 Flash to process raw receipt images, extract items, aggregate duplicates, and categorize them.
* **Predictive Expiry Engine:** Automatically calculates estimated expiration dates based on product categories.
* **Cloud Database:** Synchronizes the virtual inventory seamlessly with Supabase (PostgreSQL REST API).

## Tech Stack
* Python 3
* Google Gemini API (2.5 Flash)
* Supabase REST API