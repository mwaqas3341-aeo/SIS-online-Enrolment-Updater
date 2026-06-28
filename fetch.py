#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper (FULL RUN)
=======================================================================
Grade mapping (confirmed from live website + log analysis):

  Full school sequence: ECE | Nursery | 1 | 2 | … | 10 | 11 | 12
  (Grades 11-12 = Intermediate / Higher Secondary)

  Positional array length → grade keys:
    14 values  →  ECE, Nursery, 1-10, 11, 12   (full Higher Secondary)
    13 values  →  Nursery, 1-10, 11, 12         (HS without ECE)
    12 values  →  ECE, Nursery, 1-10            (primary/middle, both pre-primary)
    11 values  →  Nursery, 1-10                 (primary/middle, Nursery only)
    10 values  →  1-10                          (no pre-primary)
    <10 values →  1 … n                         (primary-only)
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

S = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
S.mount('https://', adapter)
S.mount('http://', adapter)
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
})

csv_lock           = threading.Lock()
DEBUG_FIRST_SCHOOL = True
_debug_printed     = False

# Full grade sequence including Intermediate (11, 12)
ALL_GRADES = ["ECE", "Nursery", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"]

FIELDS = [
    "school_id", "emis_code", "school_name", "district_id", "district",
    "tehsil_id", "tehsil", "markaz_id", "markaz",
    "total_students", "boys", "girls", "teachers",
    # Pre-primary
    "grade_ECE_boys",     "grade_ECE_girls",
    "grade_Nursery_boys", "grade_Nursery_girls",
    # Primary
    "grade_1_boys",  "grade_1_girls",
    "grade_2_boys",  "grade_2_girls",
    "grade_3_boys",  "grade_3_girls",
    "grade_4_boys",  "grade_4_girls",
    "grade_5_boys",  "grade_5_girls",
    # Middle
    "grade_6_boys",  "grade_6_girls",
    "grade_7_boys",  "grade_7_girls",
    "grade_8_boys",  "grade_8_girls",
    # Secondary
    "grade_9_boys",  "grade_9_girls",
    "grade_10_boys", "grade_10_girls",
    # Higher Secondary / Intermediate
    "grade_11_boys", "grade_11_girls",
    "grade_12_boys", "grade_12_girls",
    "etransfer_status", "scraped_at",
]

GRADE_MAP = {
    "ECE":        "ECE",
    "Nursery":    "Nursery", "nursery": "Nursery",
    "KG":         "Nursery", "katchi":  "Nursery", "Katchi": "Nursery",
    "Pre-School": "Nursery", "Prep":    "Nursery",
    "1": "1",  "2": "2",  "3": "3",  "4": "4",  "5": "5",
    "6": "6",  "7": "7",  "8": "8",  "9": "9",  "10": "10",
    "11": "11", "12": "12",
    "Class 1": "1",   "Class 2": "2",   "Class 3": "3",
    "Class 4": "4",   "Class 5": "5",   "Class 6": "6",
    "Class 7": "7",   "Class 8": "8",   "Class 9": "9",
    "Class 10": "10", "Class 11": "11", "Class 12": "12",
    "Grade 1": "1",   "Grade 2": "2",   "Grade 3": "3",
    "Grade 4": "4",   "Grade 5": "5",   "Grade 6": "6",
    "Grade 7": "7",   "Grade 8": "8",   "Grade 9": "9",
    "Grade 10": "10", "Grade 11": "11", "Grade 12": "12",
    # Intermediate labels sometimes used
    "I":  "11", "II": "12",
    "XI": "11", "XII": "12",
    "Inter I": "11", "Inter II": "12",
    "F.Sc I": "11", "F.Sc II": "12",
    "FA I": "11",   "FA II": "12",
}

PRIMARY   = ["1","2","3","4","5","6","7","8","9","10"]
INTER     = ["11","12"]


def positional_grade_keys(n):
    """
    Map array length → ordered grade key list.

    Confirmed sequences from live site:
      14 = ECE, Nursery, 1-10, 11, 12  (full Higher Secondary)
      13 = Nursery, 1-10, 11, 12       (HS without ECE)
      12 = ECE, Nursery, 1-10          (no Intermediate)
      11 = Nursery, 1-10
      10 = 1-10
      <10 = 1 … n
    """
    if   n == 14: return ["ECE", "Nursery"] + PRIMARY + INTER
    elif n == 13: return ["Nursery"] + PRIMARY + INTER
    elif n == 12: return ["ECE", "Nursery"] + PRIMARY
    elif n == 11: return ["Nursery"] + PRIMARY
    elif n == 10: return PRIMARY[:]
    elif n < 10:  return PRIMARY[:n]
    else:
        # n > 14: extend gracefully
        extras = [str(i) for i in range(13, n + 1)]
        return ["ECE", "Nursery"] + PRIMARY + INTER + extras


def to_int(value):
    if value is None: return 0
    if isinstance(value, int): return value
    if isinstance(value, float): return int(value)
    if isinstance(value, dict):
        return to_int(value.get("y") or value.get("value") or 0)
    if isinstance(value, str):
        clean = re.sub(r'[^\d]', '', value)
        return int(clean) if clean else 0
    return 0


def get_csrf():
    print("[Network] Requesting CSRF token from server...", flush=True)
    try:
        r = S.get(f"{BASE}/str/analysis", timeout=15)
        csrf = S.cookies.get("csrf_cookie_name", "")
        if not csrf:
            m = re.search(r'csrf_cookie_name["\s:\']+([a-f0-9]+)', r.text)
            if m: csrf = m.group(1)
        print(f"[Network] CSRF Token received: {csrf[:10]}...", flush=True)
        return csrf
    except Exception as e:
        print(f"[Error] Failed to connect to server: {e}", flush=True)
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
    return parse_resp(S.get(
        f"{BASE}/user/get_tehsils",
        params={"district": d_id, "selectedTehsil": "false", "all": "All", "csrf_test_name": csrf},
        timeout=15
    ))


def get_markazs(d_id, t_id, csrf):
    return parse_resp(S.get(
        f"{BASE}/user/get_markazes",
        params={"tehsil": t_id, "selectedMarkaz": "false", "all": "All", "csrf_test_name": csrf},
        timeout=15
    ))


def get_schools(d_id, t_id, m_id, csrf):
    return parse_resp(S.get(
        f"{BASE}/user/get_schools",
        params={"markaz": m_id, "selectedSchool": "false", "all": "All", "csrf_test_name": csrf},
        timeout=15
    ))


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
            "total_students": 0, "boys": 0, "girls": 0,
            "teachers": 0, "etransfer_status": "UNKNOWN", "scraped_at": ts
        }
        for g in ALL_GRADES:
            base_school[f"grade_{g}_boys"]  = 0
            base_school[f"grade_{g}_girls"] = 0
        schools_found.append(base_school)
    return schools_found


def apply_grade_data(school_info, data2):
    """
    Parse data2 (dict) and write grade values into school_info.
    Returns (ok: bool, array_len: int).
    """
    if not isinstance(data2, dict):
        return False, 0

    categories  = data2.get("categories", [])
    male_vals   = data2.get("male")   or data2.get("Male")
    female_vals = data2.get("female") or data2.get("Female")

    # Highcharts series array
    if not male_vals and "series" in data2:
        for series in data2["series"]:
            name = (series.get("name") or "").strip().lower()
            if name in ("male","boys","m"):
                male_vals = series.get("data", [])
            elif name in ("female","girls","f"):
                female_vals = series.get("data", [])

    # List-of-dicts rows
    if not male_vals and isinstance(data2.get("data"), list):
        rows = data2["data"]
        if rows and isinstance(rows[0], dict):
            categories  = [r.get("class") or r.get("grade") or r.get("category") or r.get("name") for r in rows]
            male_vals   = [to_int(r.get("male")   or r.get("boys"))  for r in rows]
            female_vals = [to_int(r.get("female") or r.get("girls")) for r in rows]

    if not male_vals or not female_vals:
        return False, 0

    n = max(len(male_vals), len(female_vals))
    grade_keys = [GRADE_MAP.get(str(c).strip()) for c in categories] if categories else positional_grade_keys(n)

    for i, g_key in enumerate(grade_keys):
        if g_key is None: continue
        if i >= len(male_vals) or i >= len(female_vals): break
        school_info[f"grade_{g_key}_boys"]  = to_int(male_vals[i])
        school_info[f"grade_{g_key}_girls"] = to_int(female_vals[i])

    return True, n


def worker_fetch_school_data(school_info, ts):
    global _debug_printed

    params = {
        "district":       school_info["district_id"],
        "tehsil":         school_info["tehsil_id"],
        "markaz":         school_info["markaz_id"],
        "school":         school_info["school_id"],
        "classes":        "",
        "s_id_emis_code": ""
    }

    # ── 1. Gender summary totals ────────────────────────────────────────────
    try:
        r1 = S.get(f"{BASE}/dashboard_revamp/get_gender_summary_pie", params=params, timeout=15)
        if r1.status_code == 200:
            data1 = r1.json()
            if isinstance(data1, dict):
                school_info["total_students"] = to_int(data1.get("total"))
                school_info["boys"]           = to_int(data1.get("male_count"))
                school_info["girls"]          = to_int(data1.get("female_count"))
    except Exception as e:
        print(f"[WARN] pie failed for school {school_info['school_id']}: {e}", flush=True)

    # ── 2. Grade-wise breakdown ─────────────────────────────────────────────
    try:
        r2 = S.get(f"{BASE}/dashboard_revamp/get_gender_bar_class", params=params, timeout=15)
        if r2.status_code == 200:
            raw   = r2.json()
            # Guard: API can return a list instead of dict for some schools
            data2 = raw if isinstance(raw, dict) else {}

            if DEBUG_FIRST_SCHOOL and not _debug_printed:
                _debug_printed = True
                male_len = len(data2.get("male") or []) if data2 else 0
                print("\n" + "=" * 65, flush=True)
                print(f"[DEBUG] Raw grade bar (first school, array_len={male_len}):", flush=True)
                print(json.dumps(raw, indent=2)[:1500], flush=True)
                print("=" * 65 + "\n", flush=True)

            if not data2:
                print(f"[WARN] Unexpected response type for school "
                      f"{school_info['school_id']}: {type(raw).__name__}", flush=True)
            else:
                ok, n = apply_grade_data(school_info, data2)
                if not ok:
                    print(f"[WARN] No grade data for school {school_info['school_id']}: "
                          f"keys={list(data2.keys())}", flush=True)
                else:
                    grade_sum = sum(
                        school_info.get(f"grade_{g}_boys",  0) +
                        school_info.get(f"grade_{g}_girls", 0)
                        for g in ALL_GRADES
                    )
                    reported = school_info.get("total_students", 0)
                    if reported > 0 and abs(grade_sum - reported) > 5:
                        print(
                            f"[WARN] Sum mismatch — school {school_info['school_id']} "
                            f"({school_info['school_name']}): "
                            f"grade_sum={grade_sum}, reported={reported}, array_len={n}",
                            flush=True
                        )

    except Exception as e:
        print(f"[WARN] grade bar failed for school {school_info['school_id']}: {e}", flush=True)

    # ── 3. Write row to CSV (thread-safe) ───────────────────────────────────
    with csv_lock:
        with open("schools.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writerow(school_info)

    return school_info


def verify_ece_school(inventory):
    """Spot-check the ATTOCK school from the screenshot to confirm ECE/Nursery mapping."""
    TARGET_EMIS = "37110221"
    match = next((s for s in inventory if s.get("emis_code") == TARGET_EMIS), None)
    candidates = [match] if match else [
        s for s in inventory if s.get("district","").upper() == "ATTOCK"
    ][:10]

    label = f"EMIS {TARGET_EMIS}" if match else "first 10 ATTOCK schools"
    print(f"\n[ECE-CHECK] Verifying {label}...", flush=True)

    for school in candidates:
        params = {
            "district":       school["district_id"],
            "tehsil":         school["tehsil_id"],
            "markaz":         school["markaz_id"],
            "school":         school["school_id"],
            "classes":        "",
            "s_id_emis_code": ""
        }
        try:
            r    = S.get(f"{BASE}/dashboard_revamp/get_gender_bar_class", params=params, timeout=15)
            data = r.json()
            if not isinstance(data, dict):
                continue
            male   = data.get("male",   [])
            female = data.get("female", [])
            n      = len(male)
            keys   = positional_grade_keys(n)

            print(f"  School : {school['school_name']} (EMIS: {school['emis_code']})", flush=True)
            print(f"  array_len={n}  →  {keys}", flush=True)
            for i, g_key in enumerate(keys):
                b  = to_int(male[i])   if i < len(male)   else 0
                f_ = to_int(female[i]) if i < len(female) else 0
                if b or f_:
                    print(f"    grade_{g_key:>7}: boys={b}  girls={f_}", flush=True)

            if n in (11, 12):
                print(f"  ✅ ECE/Nursery confirmed! (array_len={n})\n", flush=True)
                break
        except Exception as e:
            print(f"  [ECE-CHECK] Error: {e}", flush=True)


def scrape():
    ts = datetime.now(timezone.utc).isoformat()
    csrf = get_csrf()

    print("[Network] Requesting Districts list...", flush=True)
    r = S.get(f"{BASE}/user/get_districts", timeout=15)
    districts = parse_resp(r)
    print(f"[Success] Found {len(districts)} Districts.", flush=True)

    markaz_list = []
    print("\nPhase 1a: Mapping Tehsils and Markazs sequentially...", flush=True)
    for d_id, d_name in districts:
        tehsils = get_tehsils(d_id, csrf) or [("", "All")]
        print(f"  -> {d_name}: Found {len(tehsils)} tehsils", flush=True)
        for t_id, t_name in tehsils:
            markazs = get_markazs(d_id, t_id, csrf) or [("", "All")]
            for m_id, m_name in markazs:
                markaz_list.append((d_id, d_name, t_id, t_name, m_id, m_name))

    print(f"\n[Success] Mapped exactly {len(markaz_list)} Markazs.", flush=True)

    print(f"\nPhase 1b: Fetching school lists across {len(markaz_list)} Markazs concurrently...", flush=True)
    inventory         = []
    completed_markazs = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(worker_fetch_schools_in_markaz, m, csrf, ts): m for m in markaz_list}
        for future in concurrent.futures.as_completed(futures):
            completed_markazs += 1
            inventory.extend(future.result())
            if completed_markazs % 200 == 0:
                print(f"  -> Processed {completed_markazs} / {len(markaz_list)} Markazs...", flush=True)

    print(f"\nPhase 1 Complete! Discovered exactly {len(inventory)} schools.", flush=True)

    verify_ece_school(inventory)

    # Write CSV header
    with open("schools.csv", "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    # ── FULL RUN ────────────────────────────────────────────────────────────
    print(f"\nPhase 2: Fetching enrollment data for ALL {len(inventory)} schools...", flush=True)
    completed_schools = 0
    final_schools     = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
        futures = {executor.submit(worker_fetch_school_data, s, ts): s for s in inventory}
        for future in concurrent.futures.as_completed(futures):
            completed_schools += 1
            final_schools.append(future.result())
            if completed_schools % 1000 == 0:
                print(f"  -> Fetched data for {completed_schools} / {len(inventory)} schools...", flush=True)

    return final_schools, ts


if __name__ == "__main__":
    print("=" * 65, flush=True)
    print("  SIS PESRP Scraper — FULL RUN (all schools)", flush=True)
    print("=" * 65, flush=True)
    start_time = time.time()

    schools, ts = scrape()

    # ── Save JSON ───────────────────────────────────────────────────────────
    tot = sum(s.get("total_students", 0) for s in schools)
    out = {
        "scraped_at": ts,
        "source":     BASE,
        "summary": {
            "total_schools":  len(schools),
            "total_students": tot,
            "total_boys":     sum(s.get("boys", 0)     for s in schools),
            "total_girls":    sum(s.get("girls", 0)    for s in schools),
            "total_teachers": sum(s.get("teachers", 0) for s in schools),
        },
        "schools": schools,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # ── Final stats ─────────────────────────────────────────────────────────
    graded      = sum(1 for s in schools if any(
        s.get(f"grade_{g}_{sex}", 0) > 0 for g in ALL_GRADES for sex in ["boys","girls"]))
    ece_schools = sum(1 for s in schools
                      if s.get("grade_ECE_boys",0)>0 or s.get("grade_ECE_girls",0)>0)
    nur_schools = sum(1 for s in schools
                      if s.get("grade_Nursery_boys",0)>0 or s.get("grade_Nursery_girls",0)>0)
    hs_schools  = sum(1 for s in schools
                      if s.get("grade_11_boys",0)>0 or s.get("grade_11_girls",0)>0
                      or s.get("grade_12_boys",0)>0 or s.get("grade_12_girls",0)>0)

    elapsed = (time.time() - start_time) / 60
    print(f"\n{'='*65}", flush=True)
    print(f"✅ FULL RUN COMPLETE in {elapsed:.1f} minutes!", flush=True)
    print(f"{'='*65}", flush=True)
    print(f"   Total schools         : {len(schools):,}", flush=True)
    print(f"   Total students        : {tot:,}", flush=True)
    print(f"   Schools with grade data : {graded:,}", flush=True)
    print(f"   Schools with ECE      : {ece_schools:,}", flush=True)
    print(f"   Schools with Nursery  : {nur_schools:,}", flush=True)
    print(f"   Schools with Gr 11-12 : {hs_schools:,}", flush=True)
    print(f"   → schools.csv", flush=True)
    print(f"   → data.json", flush=True)