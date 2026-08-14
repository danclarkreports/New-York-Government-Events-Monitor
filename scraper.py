import os
import time
import json
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# NY State Government Event Sweeper
# Powered by Gemini

# Safely pull the API key from environment variables (GitHub Secrets)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("Error: GEMINI_API_KEY environment variable not found.")
    exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

# Define the exact structure we want Gemini to return
class EventModel(BaseModel):
    title: str = Field(description="The name of the event")
    organization: str = Field(description="The agency hosting the event")
    category: str = Field(description="Event type (e.g., Board Meeting, Public Hearing)")
    date: str = Field(description="Format: YYYY-MM-DD")
    time: str = Field(description="Time of the event (e.g., 10:00 AM)")
    location: str = Field(description="Physical address or 'Virtual'")
    summary: str = Field(description="A short 1-2 sentence description")
    link: str = Field(description="URL to the event page or agency calendar")

def load_sources():
    try:
        with open("sources.json", "r") as f:
            return json.load(f).get("sources", [])
    except Exception as e:
        print(f"Error loading sources.json: {e}")
        return []

def extract_events_with_gemini(html_content, org_name, source_url):
    """Uses Gemini to extract structured event data from raw HTML."""
    prompt = f"""
    You are an expert data extraction parser.
    Extract all future public hearings, board meetings, legislative sessions, 
    and council events from the following HTML for {org_name}.
    Ignore past events. Ignore navigation links.
    If no events are found, return an empty array.
    Ensure all links are absolute URLs; if a relative link is found, prepend {source_url}.
    """
    
    try:
        # Use structured outputs to force the model to return a JSON array of EventModels
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=[prompt, html_content[:40000]], # limit token size to avoid context bloat
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=list[EventModel],
                temperature=0.1 # Keep it very deterministic and factual
            ),
        )
        # The response.text is guaranteed to match the schema
        events_json = json.loads(response.text)
        
        # Validate that the response is actually a list
        if isinstance(events_json, list):
             return events_json
        else:
             print(f"[-] Gemini returned invalid format for {org_name}. Expected a list.")
             return []
             
    except Exception as e:
        print(f"[-] Gemini extraction failed for {org_name}: {e}")
        return []

def sweep_agencies():
    sources = load_sources()
    all_events = []
    
    # Load existing events so we don't completely overwrite if a site is temporarily down
    try:
        with open("ny_events_master.json", "r") as f:
             existing_events = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        existing_events = []
    
    # Keep track of events we've successfully updated so we can clean out old ones
    updated_orgs = set()

    for source in sources:
        if not source.get("enabled", True):
            continue
            
        print(f"[*] Sweeping {source['organization']}...")
        
        # Handle fallback lists (like MTA which heavily blocks scrapers)
        if "fallback_events" in source:
            print(f"  -> Using cached fallback events.")
            # Map fallback schema to standard schema
            for e in source["fallback_events"]:
                 all_events.append({
                    "title": e.get("title", "Event"),
                    "organization": e.get("organization", source["organization"]),
                    "category": e.get("category", "Meeting"),
                    "date": e.get("date", "TBD"),
                    "time": e.get("time", "TBD"),
                    "location": e.get("location", "TBD"),
                    "summary": e.get("summary", ""),
                    "link": e.get("link", source["url"])
                 })
            updated_orgs.add(source["organization"])
            continue
            
        try:
            # Format dynamic dates in URL
            target_url = source["url"]
            if "{from_date}" in target_url or "{to_date}" in target_url:
                today = datetime.now()
                future = today + timedelta(days=180) # Look up to 6 months ahead
                target_url = target_url.replace("{from_date}", today.strftime("%Y-%m-%d"))
                target_url = target_url.replace("{to_date}", future.strftime("%Y-%m-%d"))

            # Add simple headers to mimic a normal browser
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Connection": "keep-alive"
            }
            res = requests.get(target_url, headers=headers, timeout=20)
            
            # Clean HTML to reduce token usage and improve extraction
            soup = BeautifulSoup(res.text, 'html.parser')
            # Remove scripts, styles, and other non-content tags
            for script in soup(["script", "style", "noscript", "meta", "header", "footer"]):
                script.decompose()
                
            clean_html = soup.get_text(separator=' ', strip=True)
            
            # Extract
            events = extract_events_with_gemini(clean_html, source["organization"], source["url"])
            if events:
                 # Ensure every event has a unique ID and maps organization to agency
                 for i, evt in enumerate(events):
                     evt["id"] = f"{source['id'].lower()}-{int(time.time())}-{i}"
                     # Map 'organization' key to 'agency' key for the frontend
                     evt["agency"] = evt.get("organization", source["organization"])
                 all_events.extend(events)
                 updated_orgs.add(source["organization"])
                 print(f"  -> Found {len(events)} events.")
            else:
                 print(f"  -> No events found or extraction failed.")
                 
            # Polite rate limiting between sites
            time.sleep(3) 
            
        except Exception as e:
            print(f"[-] Failed to sweep {source['organization']}: {e}")

    # Retain events from organizations that failed to update this run to prevent data loss
    for old_event in existing_events:
         if old_event.get("organization") not in updated_orgs and old_event.get("agency") not in updated_orgs:
              all_events.append(old_event)

    # Save output
    try:
        with open("ny_events_master.json", "w") as f:
            json.dump(all_events, f, indent=2)
        print(f"\n[+] Sweep complete. Saved {len(all_events)} total events.")
    except Exception as e:
         print(f"[-] Failed to save events: {e}")

if __name__ == "__main__":
    sweep_agencies()
