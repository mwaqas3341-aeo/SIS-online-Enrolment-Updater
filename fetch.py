#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper (Multithreaded + Live CSV Writing)
=======================================================================
"""

import json
import csv
import re
import time
import requests
import threading
import concurrent.futures
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"

# --- Thread-Safe Session Configuration ---
# Increased pool_connections and pool_maxsize to handle concurrent threads
S = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
S.mount('https://', adapter)
S.mount('http://', adapter)

S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/?tab=district_quota&district=",
})

# Lock for writing to CSV safely from multiple threads
csv_lock = threading.Lock()

FIELDS = [
    "school_id", "emis_code", "school_name",
    "district_id", "district", "tehsil_id", "tehsil",
    "markaz_id", "markaz", "total_students", "boys", "girls", "teachers",
    "grade_KG_boys", "grade_KG_girls", "grade_1_boys", "grade_1_girls", 
    "grade_2_boys", "grade_2_girls", "grade_3_boys", "grade_3_girls", 
    "grade_4_boys", "grade_4_girls", "grade_5_boys", "grade_5_girls", 
    "grade_6_boys", "grade_6_girls", "grade_7_boys", "grade_7_girls", 
    "grade_8_boys", "grade_8_girls", "grade_9_boys", "grade_9_girls", 
    "grade_10_boys", "grade_10_girls", "etransfer_status", "scraped_at",
]

def to_int(value):
    if value is None: return 0
    if isinstance(value, int): return value
    if isinstance(value, float): return int(value)
    if isinstance(value, str):
        clean = re.sub(r'[^\d]', '', value)
        return int(clean) if clean else 0
    return 0

def get_csrf():
    r = S.get(f"{BASE}/str/analysis", timeout=30)
    csrf = S.cookies.get("csrf_cookie_name", "")
    if not csrf:
        m = re.search(r'csrf_cookie_name["\s:\']+([a-f0-9]+)', r.text)
        if m: csrf = m.group(1)
    return csrf

def parse_options(html_str):
    opts = []
    soup = BeautifulSoup(html_str or "", "html.parser")
    skip = {"", "0", "select", "all", "--", "select district", "select tehsil", "select markaz", "select school", "all districts", "all tehsils", "all markazs", "all schools"}
    for opt in soup.find_all("option"):
        val = (opt.get("value") or "").strip()
        name = opt.get_text(strip=True)
        if val and name.lower() not in skip:
            opts.append((val, name))
    return opts

def parse_resp(r):
    if not r or r.status_code != 200: return []
    body = r.text.strip()
    if not body: return []
    if body.startswith("{"):
        try:
            d = r.json()
            return parse_options(d.get("html") or d.get("data") or d.get("options") or "")
        except: pass
    return parse_options(body)

# --- Hierarchy Discovery Functions ---
def get_tehsils(d_id, csrf):
    return parse_resp(S.get(f"{BASE}/user/get_tehsils", params={"district": d_id, "selectedTehsil": "false", "all": "All", "csrf_test_name": csrf}, timeout=30))

def get_markazs(d_id, t_id, csrf):
    return parse_resp(S.get(f"{BASE}/user/get_markazes", params={"tehsil": t_id, "selectedMarkaz": "false", "all": "All", "csrf_test_name": csrf}, timeout=30))

def get_schools(d_id, t_id, m_id, csrf):
    return parse_resp(S.get(f"{BASE}/user/get_schools", params={"markaz": m_id, "selectedSchool": "false", "all": "All", "csrf_test_name": csrf}, timeout=30))

# --- Thread Worker for Individual Schools ---
def fetch_school_data(school_info, ts):
    """Worker function to fetch enrollment for a single school."""
    params = {
        "district": school_info["district_id"],
        "tehsil": school_info["tehsil_id"],
        "markaz": school_info["markaz_id"],
        "school": school_info["school_id"],
        "classes": "",
        "s_id_emis_code": ""
    }

    # Fetch Pie Chart Data
    try:
        r1 = S.get(f"{BASE}/dashboard_revamp/get_gender_summary_pie", params=params, timeout=20)
        if r1.status_code == 200 and isinstance(r1.json(), dict):
            data1 = r1.json()
            school_info["total_students"] = to_int(data1.get("total"))
            school_info["boys"] = to_int(data1.get("male_count"))
            school_info["girls"] = to_int(data1.get("female_count"))
    except Exception as e:
        pass # Ignore and leave defaults as 0

    # Fetch Bar Chart Data (Grades)
    try:
        r2 = S.get(f"{BASE}/dashboard_revamp/get_gender_bar_class", params=params, timeout=20)
        if r2.status_code == 200 and isinstance(r2.json(), dict):
            data2 = r2.json()
            categories = data2.get("categories", [])
            male_vals = data2.get("male", [])
            female_vals = data2.get("female", [])
            grade_map = {"ECE": "KG", "Nursery": "KG", "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7", "8": "8", "9": "9", "10": "10"}
            
            for i, cat in enumerate(categories):
                if i >= len(male_vals) or i >= len(female_vals): break
                g_key = grade_map.get(cat)
                if g_key:
                    school_info[f"grade_{g_key}_boys"] = male_vals[i]
                    school_info[f"grade_{g_key}_girls"] = female_vals[i]
    except Exception as e:
        pass

    school_info["scraped_at"] = ts

    # Write to CSV safely
    with csv_lock:
        with open("schools.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writerow(school_info)

    return school_info

# --- Main Scraping Routine ---
def scrape():
    ts = datetime.now(timezone.utc).isoformat()
    csrf = get_csrf()

    print("Phase 1: Discovering all schools (this will take a few minutes)...")
    r = S.get(f"{BASE}/user/get_districts", timeout=30)
    districts = parse_resp(r)
    
    inventory = []

    for d_id, d_name in districts:
        tehsils = get_tehsils(d_id, csrf) or [("", "All")]
        for t_id, t_name in tehsils:
            markazs = get_markazs(d_id, t_id, csrf) or [("", "All")]
            for m_id, m_name in markazs:
                school_opts = get_schools(d_id, t_id, m_id, csrf)
                for s_id, s_name in school_opts:
                    emis_code, school_name_clean = "", s_name
                    if " - " in s_name:
                        parts = s_name.split(" - ", 1)
                        emis_code = parts[0].strip()
                        school_name_clean = parts[1].strip() if len(parts) > 1 else s_name

                    # Prepare base dictionary with defaults
                    base_school = {
                        "school_id": s_id, "emis_code": emis_code, "school_name": school_name_clean,
                        "district_id": d_id, "district": d_name, "tehsil_id": t_id, "tehsil": t_name,
                        "markaz_id": m_id, "markaz": m_name, "total_students": 0, "boys": 0, "girls": 0, "teachers": 0,
                        "etransfer_status": "UNKNOWN", "scraped_at": ts
                    }
                    
                    # Initialize all grade counts to 0
                    for g in ["KG", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]:
                        base_school[f"grade_{g}_boys"] = 0
                        base_school[f"grade_{g}_girls"] = 0

                    inventory.append(base_school)
                    
    print(f"\nPhase 1 Complete. Found {len(inventory)} schools.")
    
    # Initialize the CSV with headers
    with open("schools.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()

    print("\nPhase 2: Fetching enrollment data concurrently...")
    completed = 0
    final_schools = []

    # --- Concurrent Execution ---
    # Using 20 workers is a safe balance between speed and not overwhelming the server
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        # Map the worker function over the inventory
        futures = {executor.submit(fetch_school_data, s, ts): s for s in inventory}
        
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            final_schools.append(future.result())
            
            # Print progress every 500 schools
            if completed % 500 == 0:
                print(f"  -> Progress: {completed} / {len(inventory)} schools fetched...")

    return final_schools, ts

if __name__ == "__main__":
    print("=" * 60)
    print("  SIS PESRP Scraper (Fast Multithreaded Version)")
    print("=" * 60)
    start_time = time.time()
    
    schools, ts = scrape()
    
    # Generate Final JSON
    tot = sum(s.get("total_students", 0) for s in schools)
    out = {
        "scraped_at": ts,
        "source": BASE,
        "summary": {
            "total_schools": len(schools),
            "total_students": tot,
            "total_boys": sum(s.get("boys", 0) for s in schools),
            "total_girls": sum(s.get("girls", 0) for s in schools),
            "total_teachers": sum(s.get("teachers", 0) for s in schools),
        },
        "schools": schools,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    elapsed = (time.time() - start_time) / 60
    print(f"\n✅ Finished in {elapsed:.1f} minutes!")
    print(f"schools.csv -> {len(schools)} rows")
    print(f"data.json   -> {len(schools)} schools | {tot:,} students")
