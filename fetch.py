#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper (HAR‑corrected version)
======================================================
Based on real network traffic captured from the site.
All dropdown fetches are now GET requests with proper query parameters.
Enrollment data is pulled from the dashboard_revamp endpoints.
"""

import json
import csv
import re
import time
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

BASE = "https://sis.pesrp.edu.pk"

# Session with browser‑like headers
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/?tab=district_quota&district=",
})


def get_csrf():
    """Fetch the CSRF token from the main page."""
    r = S.get(f"{BASE}/str/analysis", timeout=20)
    # The token is stored in a cookie named 'csrf_cookie_name'
    csrf = S.cookies.get("csrf_cookie_name", "")
    if not csrf:
        # Fallback: scrape from HTML if cookie not present
        m = re.search(r'csrf_cookie_name["\s:\']+([a-f0-9]+)', r.text)
        if m:
            csrf = m.group(1)
    print(f"CSRF: {csrf[:16]}..." if csrf else "CSRF: (not found)")
    return csrf


def parse_options(html_str):
    """Extract (value, name) pairs from <option> tags inside an HTML string."""
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
    """
    Extract HTML options from a response.
    Handles both plain HTML and JSON wrappers like {"html": "..."}.
    """
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
# Dropdown fetches – all use GET with specific query parameters
# ----------------------------------------------------------------------

def get_tehsils(d_id, csrf):
    """Fetch tehsils for a given district ID."""
    params = {
        "district": d_id,
        "selectedTehsil": "false",
        "all": "All",
        "csrf_test_name": csrf
    }
    r = S.get(f"{BASE}/user/get_tehsils", params=params, timeout=15)
    return parse_resp(r), csrf


def get_markazs(d_id, t_id, csrf):
    """Fetch markazs for a given tehsil ID."""
    params = {
        "tehsil": t_id,
        "selectedMarkaz": "false",
        "all": "All",
        "csrf_test_name": csrf
    }
    r = S.get(f"{BASE}/user/get_markazes", params=params, timeout=15)
    return parse_resp(r), csrf


def get_schools(d_id, t_id, m_id, csrf):
    """Fetch schools for a given markaz ID."""
    params = {
        "markaz": m_id,
        "selectedSchool": "false",
        "all": "All",
        "csrf_test_name": csrf
    }
    r = S.get(f"{BASE}/user/get_schools", params=params, timeout=15)
    return parse_resp(r), csrf


# ----------------------------------------------------------------------
# Enrollment data – from dashboard_revamp endpoints
# ----------------------------------------------------------------------

def get_enrollment(s_id, d_id, t_id, m_id, csrf):
    """
    Fetch enrollment totals and grade‑wise breakdown for a specific school.
    Returns a dictionary with keys:
      total_students, boys, girls, teachers (currently 0), grades (dict)
    """
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

    # 1. Get summary totals (gender pie)
    r = S.get(f"{BASE}/dashboard_revamp/get_gender_summary_pie",
              params=params, timeout=15)
    if r and r.status_code == 200:
        try:
            data = r.json()
            enr["total_students"] = int(data.get("total", "0").replace(",", ""))
            enr["boys"] = int(data.get("male_count", "0").replace(",", ""))
            enr["girls"] = int(data.get("female_count", "0").replace(",", ""))
        except Exception:
            pass

    # 2. Get grade‑wise breakdown (bar chart data)
    r2 = S.get(f"{BASE}/dashboard_revamp/get_gender_bar_class",
               params=params, timeout=15)
    if r2 and r2.status_code == 200:
        try:
            data = r2.json()
            categories = data.get("categories", [])   # e.g. ["ECE","Nursery","1","2",...]
            male_vals = data.get("male", [])
            female_vals = data.get("female", [])

            # Map API category names to our grade keys (KG, 1–10)
            grade_map = {
                "ECE": "KG",
                "Nursery": "KG",
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
        except Exception:
            pass

    # Note: Teacher count is not available from these endpoints.
    # You may need to find another endpoint; for now we leave it 0.
    return enr, csrf


# ----------------------------------------------------------------------
# Main scraping routine
# ----------------------------------------------------------------------

def scrape():
    ts = datetime.now(timezone.utc).isoformat()
    csrf = get_csrf()

    # Fetch districts (this endpoint still works as GET)
    r = S.get(f"{BASE}/user/get_districts", timeout=15)
    districts = parse_resp(r)
    print(f"Districts: {len(districts)}")

    schools = []

    for d_id, d_name in districts:
        print(f"\nDistrict: {d_name}")

        tehsils, csrf = get_tehsils(d_id, csrf)
        if not tehsils:
            tehsils = [("", "All")]   # fallback

        for t_id, t_name in tehsils:
            markazs, csrf = get_markazs(d_id, t_id, csrf)
            if not markazs:
                markazs = [("", "All")]

            for m_id, m_name in markazs:
                school_opts, csrf = get_schools(d_id, t_id, m_id, csrf)
                print(f"  {t_name}/{m_name}: {len(school_opts)} schools")

                for s_id, s_name in school_opts:
                    enr, csrf = get_enrollment(s_id, d_id, t_id, m_id, csrf)
                    g = enr.get("grades", {})

                    schools.append({
    "school_id": s_id,
    "emis_code": emis_code,
    "school_name": s_name,
    "district_id": d_id,        # <--- ADD THIS
    "district": d_name,
    "tehsil_id": t_id,          # <--- ADD THIS
    "tehsil": t_name,
    "markaz_id": m_id,          # <--- ADD THIS
    "markaz": m_name,
                        "total_students": enr.get("total_students", 0),
                        "boys": enr.get("boys", 0),
                        "girls": enr.get("girls", 0),
                        "teachers": enr.get("teachers", 0),
                        # Grade columns (KG, 1–10)
                        "grade_KG_boys":    g.get("grade_KG_boys", 0),
                        "grade_KG_girls":   g.get("grade_KG_girls", 0),
                        "grade_1_boys":     g.get("grade_1_boys", 0),
                        "grade_1_girls":    g.get("grade_1_girls", 0),
                        "grade_2_boys":     g.get("grade_2_boys", 0),
                        "grade_2_girls":    g.get("grade_2_girls", 0),
                        "grade_3_boys":     g.get("grade_3_boys", 0),
                        "grade_3_girls":    g.get("grade_3_girls", 0),
                        "grade_4_boys":     g.get("grade_4_boys", 0),
                        "grade_4_girls":    g.get("grade_4_girls", 0),
                        "grade_5_boys":     g.get("grade_5_boys", 0),
                        "grade_5_girls":    g.get("grade_5_girls", 0),
                        "grade_6_boys":     g.get("grade_6_boys", 0),
                        "grade_6_girls":    g.get("grade_6_girls", 0),
                        "grade_7_boys":     g.get("grade_7_boys", 0),
                        "grade_7_girls":    g.get("grade_7_girls", 0),
                        "grade_8_boys":     g.get("grade_8_boys", 0),
                        "grade_8_girls":    g.get("grade_8_girls", 0),
                        "grade_9_boys":     g.get("grade_9_boys", 0),
                        "grade_9_girls":    g.get("grade_9_girls", 0),
                        "grade_10_boys":    g.get("grade_10_boys", 0),
                        "grade_10_girls":   g.get("grade_10_girls", 0),
                        "etransfer_status": "UNKNOWN",   # you can add e‑transfer logic if needed
                        "scraped_at": ts,
                    })
                    time.sleep(0.05)   # polite delay

    return schools, ts


# ----------------------------------------------------------------------
# Save to CSV and JSON
# ----------------------------------------------------------------------

FIELDS = [
    "school_id","emis_code", "school_name", 
    "district_id", "district",    # added district_id
    "tehsil_id", "tehsil",        # added tehsil_id
    "markaz_id", "markaz",        # added markaz_id
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
    print("  SIS PESRP Scraper (HAR‑corrected)")
    print("=" * 50)
    schools, ts = scrape()
    print(f"\nTotal: {len(schools)} schools")
    save(schools, ts)
