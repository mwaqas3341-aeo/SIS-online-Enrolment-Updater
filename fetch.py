#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper (FULL RUN)
=======================================================================
KEY FINDING from HAR analysis:

  get_gender_bar_CLASS  → returns male/female arrays with NO category labels
  get_gender_bar_AREA   → returns the SAME data but WITH category labels

  Both use the same params. By switching to get_gender_bar_area we get
  exact grade labels (e.g. "ECE", "Nursery", "1" … "8") directly from
  the API — no positional guessing needed at all.

  Also confirmed: classes=0 means "All Classes" on the site.
  Passing classes=0 (not empty string) is the correct param.
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

# Long/tidy format — 1 row per grade per school
FIELDS = [
    "school_id", "emis_code", "school_name", "district_id", "district",
    "tehsil_id", "tehsil", "markaz_id", "markaz",
    "total_school_students", "total_school_boys", "total_school_girls",
    "grade_name", "male_students", "female_students", "scraped_at",
]


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
        print(f"[Network] CSRF Token: {csrf[:10]}...", flush=True)
        return csrf
    except Exception as e:
        print(f"[Error] CSRF failed: {e}", flush=True)
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
    return parse_resp(get_session().get(
        f"{BASE}/user/get_tehsils",
        params={"district": d_id, "selectedTehsil": "false", "all": "All", "csrf_test_name": csrf},
        timeout=15
    ))


def get_markazs(d_id, t_id, csrf):
    return parse_resp(get_session().get(
        f"{BASE}/user/get_markazes",
        params={"tehsil": t_id, "selectedMarkaz": "false", "all": "All", "csrf_test_name": csrf},
        timeout=15
    ))


def get_schools(d_id, t_id, m_id, csrf):
    return parse_resp(get_session().get(
        f"{BASE}/user/get_schools",
        params={"markaz": m_id, "selectedSchool": "false", "all": "All", "csrf_test_name": csrf},
        timeout=15
    ))


def worker_map_district(district_info, csrf):
    d_id, d_name = district_info
    result = []
    tehsils = get_tehsils(d_id, csrf) or [("", "All")]
    for t_id, t_name in tehsils:
        markazs = get_markazs(d_id, t_id, csrf) or [("", "All")]
        for m_id, m_name in markazs:
            result.append((d_id, d_name, t_id, t_name, m_id, m_name))
    return result


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
        schools_found.append({
            "school_id": s_id, "emis_code": emis_code, "school_name": school_name_clean,
            "district_id": d_id, "district": d_name, "tehsil_id": t_id, "tehsil": t_name,
            "markaz_id": m_id, "markaz": m_name,
            "total_school_students": 0, "total_school_boys": 0, "total_school_girls": 0,
            "scraped_at": ts
        })
    return schools_found


def worker_fetch_school_data(school_info, csv_writer):
    session = get_session()

    # ── params: classes=0 means "All Classes" (confirmed from HAR) ──────────
    params = {
        "district":       school_info["district_id"],
        "tehsil":         school_info["tehsil_id"],
        "markaz":         school_info["markaz_id"],
        "school":         school_info["school_id"],
        "classes":        "0",        # "0" = All Classes
        "s_id_emis_code": ""
    }

    # ── 1. Totals from pie chart ─────────────────────────────────────────────
    try:
        r1 = session.get(f"{BASE}/dashboard_revamp/get_gender_summary_pie",
                         params=params, timeout=15)
        if r1.status_code == 200:
            d1 = r1.json()
            if isinstance(d1, dict):
                school_info["total_school_students"] = to_int(d1.get("total"))
                school_info["total_school_boys"]     = to_int(d1.get("male_count"))
                school_info["total_school_girls"]    = to_int(d1.get("female_count"))
    except Exception:
        pass

    # ── 2. Grade breakdown from get_gender_bar_AREA (has category labels!) ──
    grades = []
    try:
        r2 = session.get(f"{BASE}/dashboard_revamp/get_gender_bar_area",
                         params=params, timeout=15)
        if r2.status_code == 200:
            raw = r2.json()

            # API returns a dict with categories, male, female arrays
            if isinstance(raw, dict):
                categories  = raw.get("categories", [])
                male_vals   = raw.get("male",   [])
                female_vals = raw.get("female", [])

                n = max(len(male_vals), len(female_vals)) if (male_vals or female_vals) else 0

                for i in range(n):
                    grade_name = str(categories[i]) if i < len(categories) else f"Class_{i+1}"
                    m = to_int(male_vals[i])   if i < len(male_vals)   else 0
                    f = to_int(female_vals[i]) if i < len(female_vals) else 0
                    grades.append({
                        "grade_name":      grade_name,
                        "male_students":   m,
                        "female_students": f,
                    })

    except Exception:
        pass

    # ── 3. Write to CSV (thread-safe) ────────────────────────────────────────
    with csv_lock:
        if not grades:
            row = {k: v for k, v in school_info.items()}
            row["grade_name"]      = "No Data"
            row["male_students"]   = 0
            row["female_students"] = 0
            csv_writer.writerow(row)
        else:
            for g in grades:
                row = {k: v for k, v in school_info.items()}
                row["grade_name"]      = g["grade_name"]
                row["male_students"]   = g["male_students"]
                row["female_students"] = g["female_students"]
                csv_writer.writerow(row)

    school_info["grades"] = grades
    return school_info


def scrape():
    ts   = datetime.now(timezone.utc).isoformat()
    csrf = get_csrf()

    print("[Network] Requesting Districts list...", flush=True)
    r         = get_session().get(f"{BASE}/user/get_districts", timeout=15)
    districts = parse_resp(r)
    print(f"[Success] Found {len(districts)} Districts.", flush=True)

    # Phase 1a: map all markazs SEQUENTIALLY (concurrent caused missing markazs)
    markaz_list = []
    print("\nPhase 1a: Mapping Tehsils and Markazs sequentially...", flush=True)
    for d_id, d_name in districts:
        tehsils = get_tehsils(d_id, csrf) or [("", "All")]
        print(f"  -> {d_name}: Found {len(tehsils)} tehsils", flush=True)
        for t_id, t_name in tehsils:
            markazs = get_markazs(d_id, t_id, csrf) or [("", "All")]
            for m_id, m_name in markazs:
                markaz_list.append((d_id, d_name, t_id, t_name, m_id, m_name))
    print(f"[Success] Mapped {len(markaz_list)} Markazs.", flush=True)

    # Phase 1b: get school lists
    print(f"\nPhase 1b: Fetching school lists across {len(markaz_list)} Markazs...", flush=True)
    inventory, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(worker_fetch_schools_in_markaz, m, csrf, ts): m for m in markaz_list}
        for future in concurrent.futures.as_completed(futures):
            done += 1
            inventory.extend(future.result())
            if done % 200 == 0:
                print(f"  -> Processed {done} / {len(markaz_list)} Markazs...", flush=True)

    print(f"\nPhase 1 Complete! Discovered {len(inventory)} schools.", flush=True)

    # Write CSV header
    with open("schools.csv", "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    # Phase 2: fetch enrollment data
    print(f"\nPhase 2: Fetching enrollment data for ALL {len(inventory)} schools (50 threads)...", flush=True)
    done_schools, final_schools = 0, []

    f_csv      = open("schools.csv", "a", newline="", encoding="utf-8")
    csv_writer = csv.DictWriter(f_csv, fieldnames=FIELDS, extrasaction="ignore")

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(worker_fetch_school_data, s, csv_writer): s for s in inventory}
        for future in concurrent.futures.as_completed(futures):
            done_schools += 1
            final_schools.append(future.result())
            if done_schools % 500 == 0:
                print(f"  -> Fetched {done_schools} / {len(inventory)} schools...", flush=True)

    f_csv.close()
    return final_schools, ts


if __name__ == "__main__":
    print("=" * 65, flush=True)
    print("  SIS PESRP Scraper — FULL RUN (get_gender_bar_area)", flush=True)
    print("=" * 65, flush=True)
    start_time = time.time()

    schools, ts = scrape()

    # Save JSON
    tot = sum(s.get("total_school_students", 0) for s in schools)
    out = {
        "scraped_at": ts, "source": BASE,
        "summary": {
            "total_schools":  len(schools),
            "total_students": tot,
            "total_boys":     sum(s.get("total_school_boys",  0) for s in schools),
            "total_girls":    sum(s.get("total_school_girls", 0) for s in schools),
        },
        "schools": schools,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # Stats
    with_grades  = sum(1 for s in schools if s.get("grades") and
                       any(g["grade_name"] != "No Data" for g in s["grades"]))
    no_data      = len(schools) - with_grades
    total_rows   = sum(len(s.get("grades", [])) for s in schools)

    # Unique grade names found across all schools
    all_grade_names = sorted(set(
        g["grade_name"]
        for s in schools for g in s.get("grades", [])
        if g["grade_name"] != "No Data"
    ))

    elapsed = (time.time() - start_time) / 60
    print(f"\n{'='*65}", flush=True)
    print(f"✅ FULL RUN COMPLETE in {elapsed:.1f} minutes!", flush=True)
    print(f"{'='*65}", flush=True)
    print(f"   Total schools      : {len(schools):,}", flush=True)
    print(f"   Total students     : {tot:,}", flush=True)
    print(f"   Schools with data  : {with_grades:,}", flush=True)
    print(f"   Schools no data    : {no_data:,}", flush=True)
    print(f"   Total CSV rows     : {total_rows:,}", flush=True)
    print(f"   Unique grade names : {all_grade_names}", flush=True)
    print(f"   → schools.csv", flush=True)
    print(f"   → data.json", flush=True)
    print(f"{'='*65}", flush=True)#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper (FULL RUN)
=======================================================================
KEY FINDING from HAR analysis:

  get_gender_bar_CLASS  → returns male/female arrays with NO category labels
  get_gender_bar_AREA   → returns the SAME data but WITH category labels

  Both use the same params. By switching to get_gender_bar_area we get
  exact grade labels (e.g. "ECE", "Nursery", "1" … "8") directly from
  the API — no positional guessing needed at all.

  Also confirmed: classes=0 means "All Classes" on the site.
  Passing classes=0 (not empty string) is the correct param.
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

# Long/tidy format — 1 row per grade per school
FIELDS = [
    "school_id", "emis_code", "school_name", "district_id", "district",
    "tehsil_id", "tehsil", "markaz_id", "markaz",
    "total_school_students", "total_school_boys", "total_school_girls",
    "grade_name", "male_students", "female_students", "scraped_at",
]


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
        print(f"[Network] CSRF Token: {csrf[:10]}...", flush=True)
        return csrf
    except Exception as e:
        print(f"[Error] CSRF failed: {e}", flush=True)
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
    return parse_resp(get_session().get(
        f"{BASE}/user/get_tehsils",
        params={"district": d_id, "selectedTehsil": "false", "all": "All", "csrf_test_name": csrf},
        timeout=15
    ))


def get_markazs(d_id, t_id, csrf):
    return parse_resp(get_session().get(
        f"{BASE}/user/get_markazes",
        params={"tehsil": t_id, "selectedMarkaz": "false", "all": "All", "csrf_test_name": csrf},
        timeout=15
    ))


def get_schools(d_id, t_id, m_id, csrf):
    return parse_resp(get_session().get(
        f"{BASE}/user/get_schools",
        params={"markaz": m_id, "selectedSchool": "false", "all": "All", "csrf_test_name": csrf},
        timeout=15
    ))


def worker_map_district(district_info, csrf):
    d_id, d_name = district_info
    result = []
    tehsils = get_tehsils(d_id, csrf) or [("", "All")]
    for t_id, t_name in tehsils:
        markazs = get_markazs(d_id, t_id, csrf) or [("", "All")]
        for m_id, m_name in markazs:
            result.append((d_id, d_name, t_id, t_name, m_id, m_name))
    return result


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
        schools_found.append({
            "school_id": s_id, "emis_code": emis_code, "school_name": school_name_clean,
            "district_id": d_id, "district": d_name, "tehsil_id": t_id, "tehsil": t_name,
            "markaz_id": m_id, "markaz": m_name,
            "total_school_students": 0, "total_school_boys": 0, "total_school_girls": 0,
            "scraped_at": ts
        })
    return schools_found


def worker_fetch_school_data(school_info, csv_writer):
    session = get_session()

    # ── params: classes=0 means "All Classes" (confirmed from HAR) ──────────
    params = {
        "district":       school_info["district_id"],
        "tehsil":         school_info["tehsil_id"],
        "markaz":         school_info["markaz_id"],
        "school":         school_info["school_id"],
        "classes":        "0",        # "0" = All Classes
        "s_id_emis_code": ""
    }

    # ── 1. Totals from pie chart ─────────────────────────────────────────────
    try:
        r1 = session.get(f"{BASE}/dashboard_revamp/get_gender_summary_pie",
                         params=params, timeout=15)
        if r1.status_code == 200:
            d1 = r1.json()
            if isinstance(d1, dict):
                school_info["total_school_students"] = to_int(d1.get("total"))
                school_info["total_school_boys"]     = to_int(d1.get("male_count"))
                school_info["total_school_girls"]    = to_int(d1.get("female_count"))
    except Exception:
        pass

    # ── 2. Grade breakdown from get_gender_bar_AREA (has category labels!) ──
    grades = []
    try:
        r2 = session.get(f"{BASE}/dashboard_revamp/get_gender_bar_area",
                         params=params, timeout=15)
        if r2.status_code == 200:
            raw = r2.json()

            # API returns a dict with categories, male, female arrays
            if isinstance(raw, dict):
                categories  = raw.get("categories", [])
                male_vals   = raw.get("male",   [])
                female_vals = raw.get("female", [])

                n = max(len(male_vals), len(female_vals)) if (male_vals or female_vals) else 0

                for i in range(n):
                    grade_name = str(categories[i]) if i < len(categories) else f"Class_{i+1}"
                    m = to_int(male_vals[i])   if i < len(male_vals)   else 0
                    f = to_int(female_vals[i]) if i < len(female_vals) else 0
                    grades.append({
                        "grade_name":      grade_name,
                        "male_students":   m,
                        "female_students": f,
                    })

    except Exception:
        pass

    # ── 3. Write to CSV (thread-safe) ────────────────────────────────────────
    with csv_lock:
        if not grades:
            row = {k: v for k, v in school_info.items()}
            row["grade_name"]      = "No Data"
            row["male_students"]   = 0
            row["female_students"] = 0
            csv_writer.writerow(row)
        else:
            for g in grades:
                row = {k: v for k, v in school_info.items()}
                row["grade_name"]      = g["grade_name"]
                row["male_students"]   = g["male_students"]
                row["female_students"] = g["female_students"]
                csv_writer.writerow(row)

    school_info["grades"] = grades
    return school_info


def scrape():
    ts   = datetime.now(timezone.utc).isoformat()
    csrf = get_csrf()

    print("[Network] Requesting Districts list...", flush=True)
    r         = get_session().get(f"{BASE}/user/get_districts", timeout=15)
    districts = parse_resp(r)
    print(f"[Success] Found {len(districts)} Districts.", flush=True)

    # Phase 1a: map all markazs SEQUENTIALLY (concurrent caused missing markazs)
    markaz_list = []
    print("\nPhase 1a: Mapping Tehsils and Markazs sequentially...", flush=True)
    for d_id, d_name in districts:
        tehsils = get_tehsils(d_id, csrf) or [("", "All")]
        print(f"  -> {d_name}: Found {len(tehsils)} tehsils", flush=True)
        for t_id, t_name in tehsils:
            markazs = get_markazs(d_id, t_id, csrf) or [("", "All")]
            for m_id, m_name in markazs:
                markaz_list.append((d_id, d_name, t_id, t_name, m_id, m_name))
    print(f"[Success] Mapped {len(markaz_list)} Markazs.", flush=True)

    # Phase 1b: get school lists
    print(f"\nPhase 1b: Fetching school lists across {len(markaz_list)} Markazs...", flush=True)
    inventory, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(worker_fetch_schools_in_markaz, m, csrf, ts): m for m in markaz_list}
        for future in concurrent.futures.as_completed(futures):
            done += 1
            inventory.extend(future.result())
            if done % 200 == 0:
                print(f"  -> Processed {done} / {len(markaz_list)} Markazs...", flush=True)

    print(f"\nPhase 1 Complete! Discovered {len(inventory)} schools.", flush=True)

    # Write CSV header
    with open("schools.csv", "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    # Phase 2: fetch enrollment data
    print(f"\nPhase 2: Fetching enrollment data for ALL {len(inventory)} schools (50 threads)...", flush=True)
    done_schools, final_schools = 0, []

    f_csv      = open("schools.csv", "a", newline="", encoding="utf-8")
    csv_writer = csv.DictWriter(f_csv, fieldnames=FIELDS, extrasaction="ignore")

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(worker_fetch_school_data, s, csv_writer): s for s in inventory}
        for future in concurrent.futures.as_completed(futures):
            done_schools += 1
            final_schools.append(future.result())
            if done_schools % 500 == 0:
                print(f"  -> Fetched {done_schools} / {len(inventory)} schools...", flush=True)

    f_csv.close()
    return final_schools, ts


if __name__ == "__main__":
    print("=" * 65, flush=True)
    print("  SIS PESRP Scraper — FULL RUN (get_gender_bar_area)", flush=True)
    print("=" * 65, flush=True)
    start_time = time.time()

    schools, ts = scrape()

    # Save JSON
    tot = sum(s.get("total_school_students", 0) for s in schools)
    out = {
        "scraped_at": ts, "source": BASE,
        "summary": {
            "total_schools":  len(schools),
            "total_students": tot,
            "total_boys":     sum(s.get("total_school_boys",  0) for s in schools),
            "total_girls":    sum(s.get("total_school_girls", 0) for s in schools),
        },
        "schools": schools,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # Stats
    with_grades  = sum(1 for s in schools if s.get("grades") and
                       any(g["grade_name"] != "No Data" for g in s["grades"]))
    no_data      = len(schools) - with_grades
    total_rows   = sum(len(s.get("grades", [])) for s in schools)

    # Unique grade names found across all schools
    all_grade_names = sorted(set(
        g["grade_name"]
        for s in schools for g in s.get("grades", [])
        if g["grade_name"] != "No Data"
    ))

    elapsed = (time.time() - start_time) / 60
    print(f"\n{'='*65}", flush=True)
    print(f"✅ FULL RUN COMPLETE in {elapsed:.1f} minutes!", flush=True)
    print(f"{'='*65}", flush=True)
    print(f"   Total schools      : {len(schools):,}", flush=True)
    print(f"   Total students     : {tot:,}", flush=True)
    print(f"   Schools with data  : {with_grades:,}", flush=True)
    print(f"   Schools no data    : {no_data:,}", flush=True)
    print(f"   Total CSV rows     : {total_rows:,}", flush=True)
    print(f"   Unique grade names : {all_grade_names}", flush=True)
    print(f"   → schools.csv", flush=True)
    print(f"   → data.json", flush=True)
    print(f"{'='*65}", flush=True)
