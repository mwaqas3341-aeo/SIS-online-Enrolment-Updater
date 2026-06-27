#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper (HAR‑corrected + retry + safe parsing)
=======================================================================
"""

import json
import csv
import re
import time
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"

# --- Session with retries ---
S = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
S.mount('https://', HTTPAdapter(max_retries=retries))

S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/?tab=district_quota&district=",
})


def to_int(value):
    """
    Safely convert a value to an integer.
    Handles strings with commas, integers, floats, and None.
    """
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        # Remove commas, spaces, and convert
        clean = re.sub(r'[^\d]', '', value)
        if clean:
            return int(clean)
    return 0


def get_csrf():
    """Fetch the CSRF token from the main page."""
    r = S.get(f"{BASE}/str/analysis", timeout=30)
    csrf = S.cookies.get("csrf_cookie_name", "")
    if not csrf:
        m = re.search(r'csrf_cookie_name["\s:\']+([a-f0-9]+)', r.text)
        if m:
            csrf = m.group(1)
    print(f"CSRF: {csrf[:16]}..." if csrf else "CSRF: (not found)")
    return csrf


def parse_options(html_str):
    """Extract (value, name) pairs from <option> tags."""
    opts = []
    soup = BeautifulSoup(html_str or "", "html.parser")
    skip = {
        "", "0", "select", "all", "--", "select district",
        "select tehsil", "select markaz", "select school",
        "all districts", "all tehsils", "all markazs", "all schools"
    }
    for opt in soup.find_all("option"):
        val = (opt.get("value") or "").strip()
        name = opt.get_text(strip=True)
        if val and name.lower() not in skip:
            opts.append((val, name))
    return opts


def parse_resp(r):
    """Extract options from response (handles JSON wrapper)."""
    if not r or r.status_code != 200:
        return []
    body = r.text.strip()
    if not body:
        return []
    if body.startswith("{"):
        try:
            d = r.json()
            html = d.get("html") or d.get("data") or d.get("options") or ""
            return parse_options(html)
        except Exception:
            pass
    return parse_options(body)


# ----------------------------------------------------------------------
# Dropdown fetches – GET with query parameters
# ----------------------------------------------------------------------

def get_tehsils(d_id, csrf):
    params = {
        "district": d_id,
        "selectedTehsil": "false",
        "all": "All",
        "csrf_test_name": csrf
    }
    r = S.get(f"{BASE}/user/get_tehsils", params=params, timeout=30)
    return parse_resp(r), csrf


def get_markazs(d_id, t_id, csrf):
    params = {
        "tehsil": t_id,
        "selectedMarkaz": "false",
        "all": "All",
        "csrf_test_name": csrf
    }
    r = S.get(f"{BASE}/user/get_markazes", params=params, timeout=30)
    return parse_resp(r), csrf


def get_schools(d_id, t_id, m_id, csrf):
    params = {
        "markaz": m_id,
        "selectedSchool": "false",
        "all": "All",
        "csrf_test_name": csrf
    }
    r = S.get(f"{BASE}/user/get_schools", params=params, timeout=30)
    return parse_resp(r), csrf


# ----------------------------------------------------------------------
# Enrollment data – with safe number conversion and type checking
# ----------------------------------------------------------------------

def get_enrollment(s_id, d_id, t_id, m_id, csrf):
    params = {
        "district": d_id,
        "tehsil": t_id,
        "markaz": m_id,
        "school": s_id,
        "classes": "",
        "s_id_emis_code": ""
    }

    enr = {
        "total_students": 0,
        "boys": 0,
        "girls": 0,
        "teachers": 0,
        "grades": {}
    }

    # Try both endpoints with up to 3 retries each
    for endpoint in [
        f"{BASE}/dashboard_revamp/get_gender_summary_pie",
        f"{BASE}/dashboard_revamp/get_gender_bar_class"
    ]:
        for attempt in range(3):
            try:
                r = S.get(endpoint, params=params, timeout=30)
                if r and r.status_code == 200:
                    data = r.json()
                    # --- CRITICAL FIX: check data type ---
                    if not isinstance(data, dict):
                        print(f"  ⚠️  {endpoint} returned unexpected type ({type(data)}), skipping")
                        # Skip this attempt – maybe try again later?
                        continue

                    if endpoint.endswith("get_gender_summary_pie"):
                        enr["total_students"] = to_int(data.get("total"))
                        enr["boys"] = to_int(data.get("male_count"))
                        enr["girls"] = to_int(data.get("female_count"))
                    else:  # gender_bar_class
                        categories = data.get("categories", [])
                        male_vals = data.get("male", [])
                        female_vals = data.get("female", [])
                        grade_map = {
                            "ECE": "KG", "Nursery": "KG",
                            "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
                            "6": "6", "7": "7", "8": "8", "9": "9", "10": "10"
                        }
                        grades = {}
                        for i, cat in enumerate(categories):
                            if i >= len(male_vals) or i >= len(female_vals):
                                break
                            grade_key = grade_map.get(cat)
                            if grade_key:
                                grades[f"grade_{grade_key}_boys"] = male_vals[i]
                                grades[f"grade_{grade_key}_girls"] = female_vals[i]
                        enr["grades"] = grades
                    break  # success, exit retry loop
                else:
                    print(f"  ⚠️  {endpoint} returned {r.status_code if r else 'None'}, retry {attempt+1}")
            except (requests.Timeout, requests.ConnectionError) as e:
                print(f"  ⚠️  {endpoint} timeout (attempt {attempt+1}): {e}")
                if attempt == 2:
                    print(f"  ❌ Skipping school {s_id} after 3 failed attempts")
                time.sleep(2 ** attempt)
            except Exception as e:
                print(f"  ⚠️  {endpoint} unexpected error: {e}, retry {attempt+1}")
                if attempt == 2:
                    print(f"  ❌ Skipping school {s_id} after 3 failed attempts")
                time.sleep(2 ** attempt)

    return enr, csrf


# ----------------------------------------------------------------------
# Main scraping routine
# ----------------------------------------------------------------------

def scrape():
    ts = datetime.now(timezone.utc).isoformat()
    csrf = get_csrf()

    r = S.get(f"{BASE}/user/get_districts", timeout=30)
    districts = parse_resp(r)
    print(f"Districts: {len(districts)}")

    schools = []

    for d_id, d_name in districts:
        print(f"\nDistrict: {d_name}")

        tehsils, csrf = get_tehsils(d_id, csrf)
        if not tehsils:
            tehsils = [("", "All")]

        for t_id, t_name in tehsils:
            markazs, csrf = get_markazs(d_id, t_id, csrf)
            if not markazs:
                markazs = [("", "All")]

            for m_id, m_name in markazs:
                school_opts, csrf = get_schools(d_id, t_id, m_id, csrf)
                print(f"  {t_name}/{m_name}: {len(school_opts)} schools")

                for s_id, s_name in school_opts:
                    # Extract EMIS code and clean name
                    emis_code = ""
                    school_name_clean = s_name
                    if " - " in s_name:
                        parts = s_name.split(" - ", 1)
                        emis_code = parts[0].strip()
                        school_name_clean = parts[1].strip() if len(parts) > 1 else s_name

                    enr, csrf = get_enrollment(s_id, d_id, t_id, m_id, csrf)
                    g = enr.get("grades", {})

                    schools.append({
                        "school_id": s_id,
                        "emis_code": emis_code,
                        "school_name": school_name_clean,
                        "district_id": d_id,
                        "district": d_name,
                        "tehsil_id": t_id,
                        "tehsil": t_name,
                        "markaz_id": m_id,
                        "markaz": m_name,
                        "total_students": enr.get("total_students", 0),
                        "boys": enr.get("boys", 0),
                        "girls": enr.get("girls", 0),
                        "teachers": enr.get("teachers", 0),
                        "grade_KG_boys": g.get("grade_KG_boys", 0),
                        "grade_KG_girls": g.get("grade_KG_girls", 0),
                        "grade_1_boys": g.get("grade_1_boys", 0),
                        "grade_1_girls": g.get("grade_1_girls", 0),
                        "grade_2_boys": g.get("grade_2_boys", 0),
                        "grade_2_girls": g.get("grade_2_girls", 0),
                        "grade_3_boys": g.get("grade_3_boys", 0),
                        "grade_3_girls": g.get("grade_3_girls", 0),
                        "grade_4_boys": g.get("grade_4_boys", 0),
                        "grade_4_girls": g.get("grade_4_girls", 0),
                        "grade_5_boys": g.get("grade_5_boys", 0),
                        "grade_5_girls": g.get("grade_5_girls", 0),
                        "grade_6_boys": g.get("grade_6_boys", 0),
                        "grade_6_girls": g.get("grade_6_girls", 0),
                        "grade_7_boys": g.get("grade_7_boys", 0),
                        "grade_7_girls": g.get("grade_7_girls", 0),
                        "grade_8_boys": g.get("grade_8_boys", 0),
                        "grade_8_girls": g.get("grade_8_girls", 0),
                        "grade_9_boys": g.get("grade_9_boys", 0),
                        "grade_9_girls": g.get("grade_9_girls", 0),
                        "grade_10_boys": g.get("grade_10_boys", 0),
                        "grade_10_girls": g.get("grade_10_girls", 0),
                        "etransfer_status": "UNKNOWN",
                        "scraped_at": ts,
                    })
                    time.sleep(0.1)

    return schools, ts


# ----------------------------------------------------------------------
# Save to CSV and JSON
# ----------------------------------------------------------------------

FIELDS = [
    "school_id", "emis_code", "school_name",
    "district_id", "district",
    "tehsil_id", "tehsil",
    "markaz_id", "markaz",
    "total_students", "boys", "girls", "teachers",
    "grade_KG_boys", "grade_KG_girls",
    "grade_1_boys", "grade_1_girls", "grade_2_boys", "grade_2_girls",
    "grade_3_boys", "grade_3_girls", "grade_4_boys", "grade_4_girls",
    "grade_5_boys", "grade_5_girls", "grade_6_boys", "grade_6_girls",
    "grade_7_boys", "grade_7_girls", "grade_8_boys", "grade_8_girls",
    "grade_9_boys", "grade_9_girls", "grade_10_boys", "grade_10_girls",
    "etransfer_status", "scraped_at",
]

def save(schools, ts):
    # CSV
    with open("schools.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(schools)

    # JSON
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

    print(f"\nschools.csv -> {len(schools)} rows")
    print(f"data.json   -> {len(schools)} schools | {tot:,} students")


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 50)
    print("  SIS PESRP Scraper (HAR‑corrected + retry + safe int + type guard)")
    print("=" * 50)
    schools, ts = scrape()
    print(f"\nTotal: {len(schools)} schools")
    save(schools, ts)