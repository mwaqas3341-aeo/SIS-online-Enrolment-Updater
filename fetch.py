#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper
=======================================================================
Grade mapping (confirmed from live website screenshot):
  Chart columns: ECE | Nursery | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10

  Positional array length → grade keys (used ONLY when the API response
  has no `categories` field):
    12 values  →  ECE, Nursery, 1–10
    11 values  →  Nursery, 1–10
    10 values  →  1–10
    <10 values →  1 … n

FIX (this version):
  The previous version silently dropped ECE/Nursery data whenever the API
  response included a `categories` array, because GRADE_MAP lookups were
  case-sensitive and didn't tolerate whitespace/punctuation variants like
  "ece", "E.C.E", "Play Group", "K.G", etc. Any category that didn't match
  exactly mapped to None and was skipped — even though the male/female
  numbers for that slot were present in the response.

  This version:
    1. Normalizes categories (lowercase, strip, collapse whitespace,
       strip trailing dots) before lookup.
    2. Expands GRADE_MAP with common ECE/Nursery synonyms.
    3. Prints a [WARN] for any category that still doesn't map, so a
       silent-drop can never happen again unnoticed.
    4. verify_ece_school() now prints the raw `categories` field (not just
       male/female arrays), so we can see definitively whether the API is
       using positional or labeled mode for that school.

TEST MODE: this run processes exactly 10 schools, with the known ECE
school (EMIS 37110221) forced into the batch (added first if not already
present), so we get real ECE/Nursery output to inspect — not just zeros
from schools that may not offer those grades.

Remove `inventory[:50]` (now [:10]) and the FORCE_TEST_EMIS forcing logic
for the full 38k-school run.
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

# Force this EMIS code into the test batch so we get a real ECE/Nursery
# sample in the 10-school test run, instead of relying on luck.
FORCE_TEST_EMIS = "37110221"

ALL_GRADES = ["ECE", "Nursery", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]

FIELDS = [
    "school_id", "emis_code", "school_name", "district_id", "district",
    "tehsil_id", "tehsil", "markaz_id", "markaz",
    "total_students", "boys", "girls", "teachers",
    "grade_ECE_boys",     "grade_ECE_girls",
    "grade_Nursery_boys", "grade_Nursery_girls",
    "grade_1_boys",  "grade_1_girls",
    "grade_2_boys",  "grade_2_girls",
    "grade_3_boys",  "grade_3_girls",
    "grade_4_boys",  "grade_4_girls",
    "grade_5_boys",  "grade_5_girls",
    "grade_6_boys",  "grade_6_girls",
    "grade_7_boys",  "grade_7_girls",
    "grade_8_boys",  "grade_8_girls",
    "grade_9_boys",  "grade_9_girls",
    "grade_10_boys", "grade_10_girls",
    "etransfer_status", "scraped_at",
]

# ── FIX: case-insensitive, whitespace/punctuation-tolerant grade map ───────
# Keys are normalized (lowercase, stripped, trailing dots removed) before
# lookup — see normalize_category(). Add more synonyms here as you discover
# them from [WARN] Unmapped category logs.
GRADE_MAP = {
    "ece": "ECE", "e c e": "ECE", "early childhood": "ECE",
    "early childhood education": "ECE",

    "nursery": "Nursery", "kg": "Nursery", "k g": "Nursery",
    "katchi": "Nursery", "pre-school": "Nursery", "pre school": "Nursery",
    "prep": "Nursery", "play group": "Nursery", "playgroup": "Nursery",
    "pg": "Nursery", "mont-1": "Nursery", "montessori": "Nursery",

    "1": "1",  "2": "2",  "3": "3",  "4": "4",  "5": "5",
    "6": "6",  "7": "7",  "8": "8",  "9": "9",  "10": "10",

    "class 1": "1",  "class 2": "2",  "class 3": "3",
    "class 4": "4",  "class 5": "5",  "class 6": "6",
    "class 7": "7",  "class 8": "8",  "class 9": "9",  "class 10": "10",

    "grade 1": "1",  "grade 2": "2",  "grade 3": "3",
    "grade 4": "4",  "grade 5": "5",  "grade 6": "6",
    "grade 7": "7",  "grade 8": "8",  "grade 9": "9",  "grade 10": "10",
}


def normalize_category(raw):
    """Lowercase, strip, collapse internal whitespace, drop trailing dots."""
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    s = re.sub(r'\s+', ' ', s)
    s = s.rstrip('.')
    return s


def positional_grade_keys(n):
    primary = ["1","2","3","4","5","6","7","8","9","10"]
    if   n == 12: return ["ECE","Nursery"] + primary
    elif n == 11: return ["Nursery"] + primary
    elif n == 10: return primary
    elif n <  10: return primary[:n]
    else:
        extras = [str(i) for i in range(11, n - 1)]
        return ["ECE","Nursery"] + primary + extras


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


def fetch_grade_bar_raw(params):
    """Return (raw_dict, array_len) for the grade bar endpoint."""
    r = S.get(f"{BASE}/dashboard_revamp/get_gender_bar_class", params=params, timeout=15)
    if r.status_code != 200:
        return None, 0
    data = r.json()
    male = data.get("male") or []
    return data, len(male)


def apply_grade_data(school_info, data2):
    """Parse data2 and write grade values into school_info. Returns True if data was found."""
    if not isinstance(data2, dict):
        return False

    categories  = data2.get("categories", [])
    male_vals   = data2.get("male")   or data2.get("Male")
    female_vals = data2.get("female") or data2.get("Female")

    if not male_vals and "series" in data2:
        for series in data2["series"]:
            name = (series.get("name") or "").strip().lower()
            if name in ("male","boys","m"):
                male_vals = series.get("data", [])
            elif name in ("female","girls","f"):
                female_vals = series.get("data", [])

    if not male_vals and isinstance(data2.get("data"), list):
        rows = data2["data"]
        if rows and isinstance(rows[0], dict):
            categories  = [r.get("class") or r.get("grade") or r.get("category") or r.get("name") for r in rows]
            male_vals   = [to_int(r.get("male")   or r.get("boys"))  for r in rows]
            female_vals = [to_int(r.get("female") or r.get("girls")) for r in rows]

    if not male_vals or not female_vals:
        return False

    n = max(len(male_vals), len(female_vals))

    # ── FIX: normalize categories before mapping, and warn on any miss ──────
    if categories:
        grade_keys = []
        for c in categories:
            norm  = normalize_category(c)
            g_key = GRADE_MAP.get(norm)
            if g_key is None:
                print(f"[WARN] Unmapped category '{c}' (normalized: '{norm}') "
                      f"for school {school_info['school_id']} — value at this "
                      f"index will be dropped. Add it to GRADE_MAP.", flush=True)
            grade_keys.append(g_key)
    else:
        grade_keys = positional_grade_keys(n)

    for i, g_key in enumerate(grade_keys):
        if g_key is None: continue
        if i >= len(male_vals) or i >= len(female_vals): break
        school_info[f"grade_{g_key}_boys"]  = to_int(male_vals[i])
        school_info[f"grade_{g_key}_girls"] = to_int(female_vals[i])

    return True


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
        data2, n = fetch_grade_bar_raw(params)

        if DEBUG_FIRST_SCHOOL and not _debug_printed:
            _debug_printed = True
            print("\n" + "=" * 65, flush=True)
            print(f"[DEBUG] Raw grade bar (first school processed, array_len={n}):", flush=True)
            print(f"[DEBUG] School: {school_info['school_name']} "
                  f"(EMIS: {school_info['emis_code']})", flush=True)
            print(f"[DEBUG] categories field: "
                  f"{data2.get('categories') if isinstance(data2, dict) else 'N/A'}", flush=True)
            print(json.dumps(data2, indent=2)[:1500], flush=True)
            print("=" * 65 + "\n", flush=True)

        if data2:
            ok = apply_grade_data(school_info, data2)
            if not ok:
                print(f"[WARN] No male/female data for school {school_info['school_id']}: "
                      f"keys={list(data2.keys())}", flush=True)
            else:
                # Sanity check
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

    # ── 3. Write row to CSV ─────────────────────────────────────────────────
    with csv_lock:
        with open("schools.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writerow(school_info)

    return school_info


# ═══════════════════════════════════════════════════════════════════════════════
# ECE/Nursery verification — finds the school from the screenshot by EMIS code
# and prints its raw API response (INCLUDING categories) so we can confirm
# whether the API uses positional or labeled grade mapping.
# ═══════════════════════════════════════════════════════════════════════════════
def verify_ece_school(inventory, csrf):
    TARGET_EMIS = FORCE_TEST_EMIS
    match = next((s for s in inventory if s.get("emis_code") == TARGET_EMIS), None)

    if not match:
        attock = [s for s in inventory if s.get("district","").upper() == "ATTOCK"]
        print(f"\n[ECE-CHECK] EMIS {TARGET_EMIS} not found. "
              f"Checking first 10 ATTOCK schools ({len(attock)} total)...", flush=True)
        candidates = attock[:10]
    else:
        print(f"\n[ECE-CHECK] Found EMIS {TARGET_EMIS}: {match['school_name']}", flush=True)
        candidates = [match]

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
            r = S.get(f"{BASE}/dashboard_revamp/get_gender_bar_class", params=params, timeout=15)
            data = r.json()
            male       = data.get("male",   [])
            female     = data.get("female", [])
            categories = data.get("categories", [])
            n = len(male)

            print(f"\n[ECE-CHECK] School: {school['school_name']} "
                  f"(EMIS: {school['emis_code']}, ID: {school['school_id']})", flush=True)
            print(f"  array_len   : {n}", flush=True)
            print(f"  categories  : {categories!r}", flush=True)  # <-- the key diagnostic
            print(f"  male        : {male}",   flush=True)
            print(f"  female      : {female}", flush=True)

            if categories:
                print(f"  → LABELED mode (categories present). Normalized mapping:", flush=True)
                for i, c in enumerate(categories):
                    norm  = normalize_category(c)
                    g_key = GRADE_MAP.get(norm)
                    flag  = "✅" if g_key else "❌ UNMAPPED"
                    print(f"    [{i}] raw='{c}' → norm='{norm}' → {g_key} {flag}", flush=True)
            else:
                keys = positional_grade_keys(n)
                print(f"  → POSITIONAL mode (no categories). mapped as: {keys}", flush=True)
                for i, g_key in enumerate(keys):
                    b  = to_int(male[i])   if i < len(male)   else 0
                    f_ = to_int(female[i]) if i < len(female) else 0
                    if b or f_:
                        print(f"    grade_{g_key:>7}: boys={b}  girls={f_}", flush=True)

            if n in (11, 12) or any(normalize_category(c) in ("ece", "nursery") for c in categories):
                print(f"  ✅ ECE/Nursery signal detected for this school!", flush=True)
                break
        except Exception as e:
            print(f"[ECE-CHECK] Error for {school['school_id']}: {e}", flush=True)


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
    inventory        = []
    completed_markazs = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(worker_fetch_schools_in_markaz, m, csrf, ts): m for m in markaz_list}
        for future in concurrent.futures.as_completed(futures):
            completed_markazs += 1
            inventory.extend(future.result())
            if completed_markazs % 200 == 0:
                print(f"  -> Processed {completed_markazs} / {len(markaz_list)} Markazs...", flush=True)

    print(f"\nPhase 1 Complete! Discovered exactly {len(inventory)} schools.", flush=True)

    # ── Verify ECE/Nursery mapping against known school ─────────────────────
    verify_ece_school(inventory, csrf)

    # ── TEST MODE: exactly 10 schools, with the known ECE school forced in ──
    # Change to `test_inventory = inventory` for the full run, and remove
    # the FORCE_TEST_EMIS block below.
    forced = [s for s in inventory if s.get("emis_code") == FORCE_TEST_EMIS]
    rest   = [s for s in inventory if s.get("emis_code") != FORCE_TEST_EMIS]
    test_inventory = (forced + rest)[:10]

    print(f"\n[TEST MODE] Limiting to {len(test_inventory)} of {len(inventory)} schools "
          f"(forced EMIS {FORCE_TEST_EMIS}: {'included' if forced else 'NOT FOUND'}).", flush=True)
    # ────────────────────────────────────────────────────────────────────────

    with open("schools.csv", "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    print("\nPhase 2: Fetching enrollment data concurrently...", flush=True)
    completed_schools = 0
    final_schools     = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(worker_fetch_school_data, s, ts): s for s in test_inventory}
        for future in concurrent.futures.as_completed(futures):
            completed_schools += 1
            final_schools.append(future.result())
            if completed_schools % 10 == 0:
                print(f"  -> Fetched data for {completed_schools} / {len(test_inventory)} schools...", flush=True)

    return final_schools, ts


if __name__ == "__main__":
    print("=" * 65, flush=True)
    print("  SIS PESRP Scraper — TEST MODE (10 schools, ECE-forced)", flush=True)
    print("=" * 65, flush=True)
    start_time = time.time()

    schools, ts = scrape()

    tot = sum(s.get("total_students", 0) for s in schools)
    out = {
        "scraped_at": ts, "source": BASE, "test_mode": True,
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

    graded      = sum(1 for s in schools if any(
        s.get(f"grade_{g}_{sex}", 0) > 0 for g in ALL_GRADES for sex in ["boys","girls"]))
    ece_schools = sum(1 for s in schools if s.get("grade_ECE_boys",0)>0 or s.get("grade_ECE_girls",0)>0)
    nur_schools = sum(1 for s in schools if s.get("grade_Nursery_boys",0)>0 or s.get("grade_Nursery_girls",0)>0)

    print(f"\n📊 Sanity check:", flush=True)
    print(f"   {graded}/{len(schools)} schools have non-zero grade data", flush=True)
    print(f"   {ece_schools}/{len(schools)} schools have ECE students", flush=True)
    print(f"   {nur_schools}/{len(schools)} schools have Nursery students", flush=True)

    if schools:
        s = schools[0]
        print(f"\n📋 Sample — {s['school_name']} (ID: {s['school_id']})", flush=True)
        print(f"   Total: {s['total_students']}  Boys: {s['boys']}  Girls: {s['girls']}", flush=True)
        for g in ALL_GRADES:
            b  = s.get(f"grade_{g}_boys",  0)
            f_ = s.get(f"grade_{g}_girls", 0)
            if b or f_:
                print(f"   Grade {g:>7}: boys={b}  girls={f_}", flush=True)

    elapsed = (time.time() - start_time) / 60
    print(f"\n✅ Finished in {elapsed:.1f} minutes!", flush=True)
    print(f"   → schools.csv  (rows: {len(schools)})", flush=True)
    print(f"   → data.json", flush=True)