#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper (50-THREAD)
Format: Long/Tidy Data (1 Row per Grade per School)
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

thread_local = threading.local()

def get_session():
    if not hasattr(thread_local, "session"):
        s = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504, 429])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
        s.mount('https://', adapter)
        s.mount('http://', adapter)
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        })
        thread_local.session = s
    return thread_local.session

csv_lock = threading.Lock()

# ── NEW: LONG FORMAT FIELDS ────────────────────────────────────────────────
FIELDS = [
    "school_id", "emis_code", "school_name", "district_id", "district",
    "tehsil_id", "tehsil", "markaz_id", "markaz",
    "total_school_students", "total_school_boys", "total_school_girls",
    "grade_name", "male_students", "female_students", "scraped_at"
]
# ───────────────────────────────────────────────────────────────────────────

def to_int(value):
    if value is None: return 0
    if isinstance(value, int): return value
    if isinstance(value, float): return int(value)
    if isinstance(value, dict): return to_int(value.get("y") or value.get("value") or 0)
    if isinstance(value, str):
        clean = re.sub(r'[^\d]', '', value)
        return int(clean) if clean else 0
    return 0

def get_csrf():
    session = get_session()
    try:
        r = session.get(f"{BASE}/str/analysis", timeout=15)
        csrf = session.cookies.get("csrf_cookie_name", "")
        if not csrf:
            m = re.search(r'csrf_cookie_name["\s:\']+([a-f0-9]+)', r.text)
            if m: csrf = m.group(1)
        return csrf
    except Exception:
        return ""

def parse_options(html_str):
    opts = []
    soup = BeautifulSoup(html_str or "", "html.parser")
    skip = {
        "", "0", "select", "all", "--",
        "select district", "select tehsil", "select markaz", "select school",
        "all districts", "all tehsils", "all markazs", "all schools"
    }
    for opt in soup.find_all("option"):
        val  = (opt.get("value") or "").strip()
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

def get_tehsils(d_id, csrf):
    return parse_resp(get_session().get(f"{BASE}/user/get_tehsils", params={"district": d_id, "selectedTehsil": "false", "all": "All", "csrf_test_name": csrf}, timeout=15))

def get_markazs(d_id, t_id, csrf):
    return parse_resp(get_session().get(f"{BASE}/user/get_markazes", params={"tehsil": t_id, "selectedMarkaz": "false", "all": "All", "csrf_test_name": csrf}, timeout=15))

def get_schools(d_id, t_id, m_id, csrf):
    return parse_resp(get_session().get(f"{BASE}/user/get_schools", params={"markaz": m_id, "selectedSchool": "false", "all": "All", "csrf_test_name": csrf}, timeout=15))

def worker_map_district(district_info, csrf):
    d_id, d_name = district_info
    local_markaz_list = []
    
    tehsils = get_tehsils(d_id, csrf) or [("", "All")]
    for t_id, t_name in tehsils:
        markazs = get_markazs(d_id, t_id, csrf) or [("", "All")]
        for m_id, m_name in markazs:
            local_markaz_list.append((d_id, d_name, t_id, t_name, m_id, m_name))
            
    return local_markaz_list

def worker_fetch_schools_in_markaz(markaz_info, csrf, ts):
    d_id, d_name, t_id, t_name, m_id, m_name = markaz_info
    school_opts = get_schools(d_id, t_id, m_id, csrf)
    schools_found = []
    for s_id, s_name in school_opts:
        emis_code, school_name_clean = "", s_name
        if " - " in s_name:
            parts = s_name.split(" - ", 1)
            emis_code         = parts[0].strip()
            school_name_clean = parts[1].strip() if len(parts) > 1 else s_name
        base_school = {
            "school_id": s_id, "emis_code": emis_code, "school_name": school_name_clean,
            "district_id": d_id, "district": d_name, "tehsil_id": t_id, "tehsil": t_name,
            "markaz_id": m_id, "markaz": m_name,
            "total_school_students": 0, "total_school_boys": 0, "total_school_girls": 0,
            "scraped_at": ts
        }
        schools_found.append(base_school)
    return schools_found

# ── NEW EXTRACTOR: Captures exact labels, no guessing, no missing data ──
def extract_grades(data2):
    grades = []
    if not data2: return grades
    
    categories, male_vals, female_vals = [], [], []

    if isinstance(data2, list) and len(data2) > 0 and isinstance(data2[0], dict):
        categories  = [str(r.get("class") or r.get("grade") or r.get("name") or "Unknown") for r in data2]
        male_vals   = [to_int(r.get("male")   or r.get("boys") or r.get("m"))  for r in data2]
        female_vals = [to_int(r.get("female") or r.get("girls") or r.get("f")) for r in data2]

    elif isinstance(data2, dict):
        if "categories" in data2: categories = data2["categories"]
        elif "labels" in data2: categories = data2["labels"]
        elif "xAxis" in data2 and isinstance(data2["xAxis"], dict):
            categories = data2["xAxis"].get("categories", [])
            
        male_vals   = data2.get("male")   or data2.get("Male") or []
        female_vals = data2.get("female") or data2.get("Female") or []
        
        if (not male_vals or not female_vals) and "series" in data2:
            for series in data2["series"]:
                name = str(series.get("name", "")).strip().lower()
                data_array = series.get("data", [])
                
                if data_array and isinstance(data_array[0], dict):
                    if not categories: 
                        categories = [str(item.get("name", "Unknown")) for item in data_array]
                    vals = [to_int(item.get("y") or item.get("value")) for item in data_array]
                else:
                    vals = [to_int(v) for v in data_array]
                    
                if name in ("male", "boys", "m"): male_vals = vals
                elif name in ("female", "girls", "f"): female_vals = vals

    n = max(len(male_vals), len(female_vals))
    
    # Absolute fallback to prevent data loss if server sends NO labels
    if not categories and n > 0:
        categories = [f"Class {i+1}" for i in range(n)]

    for i in range(n):
        c = str(categories[i]) if i < len(categories) else f"Unknown_{i}"
        m = to_int(male_vals[i]) if i < len(male_vals) else 0
        f = to_int(female_vals[i]) if i < len(female_vals) else 0
        
        # Only add to list if there is an actual label or student count
        if c != "Unknown" or m > 0 or f > 0:
            grades.append({"grade_name": c, "male_students": m, "female_students": f})

    return grades
# ───────────────────────────────────────────────────────────────────────────

def worker_fetch_school_data(school_info, ts, csv_writer):
    session = get_session() 
    params = {
        "district":       school_info["district_id"],
        "tehsil":         school_info["tehsil_id"],
        "markaz":         school_info["markaz_id"],
        "school":         school_info["school_id"],
        "classes":        "",
        "s_id_emis_code": ""
    }

    try:
        r1 = session.get(f"{BASE}/dashboard_revamp/get_gender_summary_pie", params=params, timeout=15)
        if r1.status_code == 200:
            data1 = r1.json()
            if isinstance(data1, dict):
                school_info["total_school_students"] = to_int(data1.get("total"))
                school_info["total_school_boys"]     = to_int(data1.get("male_count"))
                school_info["total_school_girls"]    = to_int(data1.get("female_count"))
    except Exception: pass

    grades = []
    try:
        r2 = session.get(f"{BASE}/dashboard_revamp/get_gender_bar_class", params=params, timeout=15)
        if r2.status_code == 200:
            raw = r2.json()
            data2 = {"data": raw} if isinstance(raw, list) else (raw if isinstance(raw, dict) else {})
            grades = extract_grades(data2)
    except Exception: pass

    school_info["grades"] = grades # Save to the dict for the JSON file later

    # Write data to CSV (1 Row per Grade!)
    with csv_lock:
        if not grades:
            # If the school has absolutely no grade data on the website, write one empty row
            row = school_info.copy()
            del row["grades"] # Remove the list object before writing to CSV
            row["grade_name"]      = "No Data"
            row["male_students"]   = 0
            row["female_students"] = 0
            csv_writer.writerow(row)
        else:
            # Write a row for EVERY class
            for g in grades:
                row = school_info.copy()
                del row["grades"]
                row["grade_name"]      = g["grade_name"]
                row["male_students"]   = g["male_students"]
                row["female_students"] = g["female_students"]
                csv_writer.writerow(row)

    return school_info

def scrape():
    ts = datetime.now(timezone.utc).isoformat()
    csrf = get_csrf()

    print("[Network] Requesting Districts list...", flush=True)
    r = get_session().get(f"{BASE}/user/get_districts", timeout=15)
    districts = parse_resp(r)

    markaz_list = []
    print("\nPhase 1a: Mapping Tehsils and Markazs concurrently...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(worker_map_district, d, csrf): d for d in districts}
        for future in concurrent.futures.as_completed(futures):
            markaz_list.extend(future.result())
            
    print(f"[Success] Mapped {len(markaz_list)} Markazs in parallel.", flush=True)

    print(f"\nPhase 1b: Fetching school lists across {len(markaz_list)} Markazs...", flush=True)
    inventory, completed_markazs = [], 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(worker_fetch_schools_in_markaz, m, csrf, ts): m for m in markaz_list}
        for future in concurrent.futures.as_completed(futures):
            completed_markazs += 1
            inventory.extend(future.result())
            if completed_markazs % 200 == 0:
                print(f"  -> Processed {completed_markazs} / {len(markaz_list)} Markazs...", flush=True)

    print(f"\nPhase 1 Complete! Discovered exactly {len(inventory)} schools.", flush=True)

    with open("schools_tidy.csv", "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    print(f"\nPhase 2: Fetching enrollment data for ALL {len(inventory)} schools (AT 50 THREADS)...", flush=True)
    completed_schools, final_schools = 0, []

    f_csv = open("schools_tidy.csv", "a", newline="", encoding="utf-8")
    csv_writer = csv.DictWriter(f_csv, fieldnames=FIELDS, extrasaction="ignore")

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(worker_fetch_school_data, s, ts, csv_writer): s for s in inventory}
        for future in concurrent.futures.as_completed(futures):
            completed_schools += 1
            final_schools.append(future.result())
            if completed_schools % 500 == 0:
                print(f"  -> Fetched data for {completed_schools} / {len(inventory)} schools...", flush=True)

    f_csv.close()
    return final_schools, ts

if __name__ == "__main__":
    print("=" * 65, flush=True)
    print("  SIS PESRP Scraper — Long/Tidy Format Output", flush=True)
    print("=" * 65, flush=True)
    start_time = time.time()

    schools, ts = scrape()

    # The JSON output will now cleanly contain an array of classes for every school
    tot = sum(s.get("total_school_students", 0) for s in schools)
    out = {
        "scraped_at": ts,
        "source":     BASE,
        "summary": {
            "total_schools":  len(schools),
            "total_students": tot,
            "total_boys":     sum(s.get("total_school_boys", 0)  for s in schools),
            "total_girls":    sum(s.get("total_school_girls", 0) for s in schools),
        },
        "schools": schools,
    }
    
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    elapsed = (time.time() - start_time) / 60
    print(f"\n{'='*65}", flush=True)
    print(f"✅ FULL RUN COMPLETE in {elapsed:.1f} minutes!", flush=True)
    print(f"   Output saved to: schools_tidy.csv and data.json")
    print(f"{'='*65}", flush=True)
