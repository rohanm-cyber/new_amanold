import os
import time
import re
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright
from datetime import datetime

# ==========================================
# CONFIGURATION
# ==========================================
CREDENTIALS_JSON = "second-hold-502307-f9-ef85df52925f.json" 
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1v6YeErYNAtoaq5KBdES21IMnDbKvQw6qVny0VaBML94/edit?gid=0#gid=0" 
BATCH_SIZE = 15  
COOLDOWN_BETWEEN_BATCHES = 30  

# ==========================================
# GOOGLE SHEETS CONNECTOR
# ==========================================
def connect_google_sheet():
    """Connects to Google Sheets using service account credentials."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if not os.path.exists(CREDENTIALS_JSON):
        raise FileNotFoundError(
            f"Could not find '{CREDENTIALS_JSON}'. Please place your Google Service Account "
            f"JSON file in this directory and name it '{CREDENTIALS_JSON}'."
        )
    
    creds = Credentials.from_service_account_file(CREDENTIALS_JSON, scopes=scopes)
    client = gspread.authorize(creds)
    
    # Sheet open karein via URL
    try:
        spreadsheet = client.open_by_url(GOOGLE_SHEET_URL)
        return spreadsheet
    except Exception as e:
        raise Exception(f"Failed to open Google Sheet. Check URL or Share access with client email: {e}")

def get_asins_from_sheet(spreadsheet):
    """Reads URLs/ASINs directly from Column A and extracts clean data."""
    try:
        sheet = spreadsheet.worksheet("Input")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.get_worksheet(0)
        
    all_values = sheet.col_values(1)  # Column A 
    
    asins = []
    for val in all_values:
        if val:
            clean_item = str(val).strip()
            if clean_item.lower() in ["asin", "asins", "product id", "sku", "input", "link", "url"]:
                continue
            asins.append(clean_item)
            
    return asins

def init_output_sheet(spreadsheet):
    """Ensures an 'Output' worksheet exists with proper headers."""
    headers = [
        "Timestamp",
        "ASIN",
        "Total Reviews",
        "Average Rating",
        "5 %",
        "4 %",
        "3 %",
        "2 %",
        "1 %"
    ]

    try:
        output_sheet = spreadsheet.worksheet("Output")
    except gspread.exceptions.WorksheetNotFound:
        output_sheet = spreadsheet.add_worksheet(
            title="Output",
            rows="1000",
            cols="10"
        )
        output_sheet.append_row(headers)

    return output_sheet
# ==========================================
# AMAZON SCRAPER ENGINE
# ==========================================
def scrape_amazon_asin(page, target_input):
    """Cleans messy/suspicious URLs, extracts ASIN, and scrapes safely."""
    clean_input = target_input.strip()

    # Capture any standard 10-character Amazon identifier starting with 'B' or digits
    asin_match = re.search(r'\b(B[A-Z0-9]{9})\b', clean_input, re.I)
    if not asin_match:
        asin_match = re.search(r'\b([A-Z0-9]{10})\b', clean_input, re.I)
        
    if asin_match:
        clean_asin = asin_match.group(1).upper()
    else:
        clean_asin = clean_input.upper()

    # Dynamically extract and preserve the exact regional marketplace domain
    domain = "www.amazon.com"
    if "amazon." in clean_input.lower():
        temp_input = clean_input
        if not temp_input.startswith(("http://", "https://")):
            temp_input = "https://" + temp_input
        from urllib.parse import urlparse
        parsed_domain = urlparse(temp_input).netloc
        if parsed_domain:
            domain = parsed_domain

    url = f"https://{domain}/dp/{clean_asin}"
        
    try:
        print("=" * 50)
        print("INPUT :", target_input)
        print("FINAL URL :", url)
        print("=" * 50)
        
        # Internal Retry Loop for stable navigation under CI/CD workloads
        max_retries = 3
        nav_success = False
        for attempt in range(1, max_retries + 1):
            try:
                # Switched to 'load' to track complex dynamic location redirects cleanly
                page.goto(url, wait_until="load", timeout=30000)
                nav_success = True
                break
            except Exception as nav_e:
                print(f"Navigation attempt {attempt} failed for ASIN {clean_asin}: {nav_e}")
                if attempt < max_retries:
                    time.sleep(3)
                    
        if not nav_success:
            return {"ASIN": clean_asin, "Total Reviews": "Error: Navigation Timeout", "Average Rating": "N/A", "5★ %": "N/A", "4★ %": "N/A", "3★ %": "N/A", "2★ %": "N/A", "1★ %": "N/A"}

        print(page.url)
        print(page.title())
        time.sleep(2)
        
        page_title = page.title().lower()
        page_url = page.url.lower()

        # Non-blocking automation-compatible CAPTCHA tracking
        if "robot check" in page_title or "something went wrong" in page_title or page.locator("input[placeholder='Type characters']").count() > 0:
            print(f"Amazon CAPTCHA/Robot check triggered for ASIN {clean_asin}!")
            return {"ASIN": clean_asin, "Total Reviews": "Error: CAPTCHA Detected", "Average Rating": "N/A", "5★ %": "N/A", "4★ %": "N/A", "3★ %": "N/A", "2★ %": "N/A", "1★ %": "N/A"}

        # Intercept unexpected security challenge or authentication requests
        if "ap/signin" in page_url or "sign in" in page_title or "sign-in" in page_title:
            print(f"Amazon authentication intercept triggered for ASIN {clean_asin}!")
            return {"ASIN": clean_asin, "Total Reviews": "Error: Auth Wall Redirect", "Average Rating": "N/A", "5★ %": "N/A", "4★ %": "N/A", "3★ %": "N/A", "2★ %": "N/A", "1★ %": "N/A"}

        # Dynamic template handling for deleted entries or 'Dogs of Amazon' crash landings
        if "page not found" in page_title or "dogs of amazon" in page_title or page.locator("#noResultsTitle").count() > 0:
            print(f"Product variant entry not active/found for ASIN {clean_asin}.")
            return {"ASIN": clean_asin, "Total Reviews": "N/A", "Average Rating": "N/A", "5★ %": "N/A", "4★ %": "N/A", "3★ %": "N/A", "2★ %": "N/A", "1★ %": "N/A"}

        # Extract Average Rating
        avg_rating = "N/A"
        rating_selectors = ["#averageCustomerReviews span.a-icon-alt", "span[data-hook='rating-out-of-text']", "#acrPopover", "span.a-icon-alt"]
        for selector in rating_selectors:
            element = page.locator(selector).first
            if element.count() > 0:
                rating_text = element.inner_text() or element.get_attribute("title") or ""
                match = re.search(r"(\d+(\.\d+)?) out of 5", rating_text)
                if match:
                    avg_rating = match.group(1)
                    break

        # Extract Total Review Count
        total_reviews = "N/A"
        review_selectors = ["#acrCustomerReviewText", "span[data-hook='total-review-count']", "#acrCustomerReviewLink"]
        for selector in review_selectors:
            element = page.locator(selector).first
            if element.count() > 0:
                review_text = element.inner_text().lower()
                if review_text:
                    total_reviews = (review_text.replace("ratings", "").replace("global ratings", "").replace("reviews", "").strip())
                    break

        # Extract Star Percentages (Dual-Layer Strategy)
        distributions = {"5★ %": "N/A", "4★ %": "N/A", "3★ %": "N/A", "2★ %": "N/A", "1★ %": "N/A"}
        histogram_selectors = ["#histogramTable", "[data-hook='histogram-container']", ".cm-cr-histogram", ".a-histogram-row"]
        combined_text = ""
        
        for sel in histogram_selectors:
            loc = page.locator(sel)
            for j in range(loc.count()):
                combined_text += " " + (loc.nth(j).inner_text() or "")
                combined_text += " " + (loc.nth(j).get_attribute("aria-label") or "")
                combined_text += " " + (loc.nth(j).get_attribute("title") or "")

        for star in range(1, 6):
            pattern = rf"{star}\s*stars?\b.*?(\d+)\s*%"
            match = re.search(pattern, combined_text, re.IGNORECASE | re.DOTALL)
            if match:
                distributions[f"{star}★ %"] = match.group(1)

        # Fallback Check if missing
        for star in range(1, 6):
            if distributions[f"{star}★ %"] == "N/A":
                fallback_locators = [
                    page.locator(f"tr:has-text('{star} star')"),
                    page.locator(f"tr:has-text('{star}★')"),
                    page.locator(f"li:has-text('{star} star')"),
                    page.locator(f".a-histogram-row:has-text('{star}')")
                ]
                for loc in fallback_locators:
                    if loc.count() > 0:
                        for k in range(loc.count()):
                            txt = loc.nth(k).inner_text() or ""
                            aria = loc.nth(k).get_attribute("aria-label") or ""
                            title = loc.nth(k).get_attribute("title") or ""
                            row_combined = f"{txt} {aria} {title}"
                            pct_match = re.search(r"(\d+)\s*%", row_combined)
                            if pct_match:
                                distributions[f"{star}★ %"] = pct_match.group(1)
                                break
                    if distributions[f"{star}★ %"] != "N/A":
                        break

        return {"ASIN": clean_asin, "Total Reviews": total_reviews, "Average Rating": avg_rating, **distributions}

    except Exception as e:
        print(f"Error reading ASIN {clean_asin}: {str(e)}")
        return {"ASIN": clean_asin, "Total Reviews": "Error", "Average Rating": "N/A", "5★ %": "N/A", "4★ %": "N/A", "3★ %": "N/A", "2★ %": "N/A", "1★ %": "N/A"}

# ==========================================
# CORE PIPELINE WITH BATCHING (TUKDE PROCESSING)
# ==========================================
def main():
    print("Connecting to Google Sheets...")
    try:
        spreadsheet = connect_google_sheet()
        asins = get_asins_from_sheet(spreadsheet)
        output_sheet = init_output_sheet(spreadsheet)
        print(f"Successfully loaded {len(asins)} items from Google Sheet.")
    except Exception as e:
        print(f"Initialization/Google Sheet Error: {e}")
        print(" REMINDER: Make sure you have shared your Google Sheet with the client_email found inside your credentials.json file!")
        return

    asin_batches = [asins[i:i + BATCH_SIZE] for i in range(0, len(asins), BATCH_SIZE)]
    total_batches = len(asin_batches)

    with sync_playwright() as p:
        # Activated headless=True for execution within headless GitHub Actions environments
        browser = p.chromium.launch(headless=True)
        
        for batch_idx, batch in enumerate(asin_batches):
            print(f"\n---  PROCESSING BATCH {batch_idx + 1} OF {total_batches} ({len(batch)} items) ---")
            
            # Rebuilt fingerprint using modern user-agent signatures and broad, standardized headers
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Upgrade-Insecure-Requests": "1"
                }
            )
            page = context.new_page()
            
            for item in batch:
                print(f"Scraping data for: {item[:40]}...")
                data = scrape_amazon_asin(page, item)
                
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                row_to_append = [
                    timestamp,
                    data["ASIN"],
                    data["Total Reviews"],
                    data["Average Rating"],
                    data["5★ %"],
                    data["4★ %"],
                    data["3★ %"],
                    data["2★ %"],
                    data["1★ %"]
                ]
                
                output_sheet.append_row(row_to_append)
                print(f" Saved clean data for ASIN: {data['ASIN']} directly to Google Sheets.")
                time.sleep(3)  # Individual delay
            
            context.close()
            
            if batch_idx < total_batches - 1:
                print(f" Batch {batch_idx + 1} complete. Sleeping for {COOLDOWN_BETWEEN_BATCHES} seconds to process next batch safely...")
                time.sleep(COOLDOWN_BETWEEN_BATCHES)

        browser.close()
    print("\n Process complete! All data has been synced to your Google Sheet.")

if __name__ == "__main__":
    main()
