#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper (FULL RUN, per-district JSON output)
=======================================================================
KEY FINDING from HAR analysis:

  get_gender_bar_CLASS  -> returns male/female arrays with NO category labels
  get_gender_bar_AREA   -> returns the SAME data but WITH category labels

  Both use the same params. By switching to get_gender_bar_area we get
  exact grade labels (e.g. "ECE", "Nursery", "1" ... "8") directly from
  the API - no positional guessing needed at all.

  Also confirmed: classes=0 means "All Classes" on the site.
  Passing classes=0 (not empty string) is the correct param.

OUTPUT STRUCTURE (changed from the original single data.json):

  data/index.json              -> master index: one entry per district
                                   (name, id, slug, filename, counts,
                                   scraped_at) plus global totals.
  data/<district_slug>.json    -> full school list + grade breakdown
                                   for ONE district only.

  The frontend loads data/index.json first (small, fast) to build the
  district picker, then loads only the selected district's JSON file.
  No combined data.json and no schools.csv are written anymore - CSV
  export is done client-side from whichever district JSON is loaded.
"""

import json
import os
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
DATA_DIR = "data"

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


def to_int(value):
    if value is None: return 0
    if isinstance(value, int): return value
    if isinstance(value, float): return int(value)
    if isinstance(value, dict): return to_int(value.get("y") or value.get("value") or 0)
    if isinstance(value, str):
        clean = re.sub(r'[^\d]', '', value)
        return int(clean) if clean else 0
    return 0


def slugify(name):
    """Turn a district name into a safe filename slug, e.g. 'Rahim Yar Khan' -> 'rahim_yar_khan'."""
    s = (name or "").strip().lower()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s or "unknown"


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
        except Exception:
            pass
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


def get_schools(d_id, t_id, m_id, csrf, retries=3):
    """Fetch schools for one markaz, retrying on empty/failed responses.

    An empty result can mean either "this markaz genuinely has 0 schools"
    or "the request silently failed under concurrent load" (the same
    issue that caused missing markazs back in Phase 1a). We retry a few
    times with a short backoff before trusting an empty result as real.
    """
    for attempt in range(retries):
        opts = parse_resp(get_session().get(
            f"{BASE}/user/get_schools",
            params={"markaz": m_id, "selectedSchool": "false", "all": "All", "csrf_test_name": csrf},
            timeout=15
        ))
        if opts:
            return opts
        if attempt < retries - 1:
            time.sleep(0.5 * (attempt + 1))
    return []


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


def worker_fetch_school_data(school_info):
    session = get_session()

    # params: classes=0 means "All Classes" (confirmed from HAR)
    params = {
        "district":       school_info["district_id"],
        "tehsil":         school_info["tehsil_id"],
        "markaz":         school_info["markaz_id"],
        "school":         school_info["school_id"],
        "classes":        "0",        # "0" = All Classes
        "s_id_emis_code": ""
    }

    # 1. Totals from pie chart
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

    # 2. Grade breakdown from get_gender_bar_AREA (has category labels!)
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

    if not grades:
        grades = [{"grade_name": "No Data", "male_students": 0, "female_students": 0}]

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
    # NOTE: lowered from 20 -> 8 workers. The same silent-failure-under-load
    # issue that forced Phase 1a (tehsils/markazs) to go sequential also
    # affects this phase: get_schools() returns [] on a failed/empty
    # response with no way to tell that apart from "genuinely 0 schools".
    # High-markaz districts (GUJRAT, GUJRANWALA, MIANWALI, HAFIZABAD,
    # NANKANA SAHIB) were losing 10-20% of schools this way. Combined with
    # the retry added in get_schools(), lowering concurrency here reduces
    # how often that silent failure gets triggered in the first place.
    print(f"\nPhase 1b: Fetching school lists across {len(markaz_list)} Markazs...", flush=True)
    inventory, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(worker_fetch_schools_in_markaz, m, csrf, ts): m for m in markaz_list}
        for future in concurrent.futures.as_completed(futures):
            done += 1
            inventory.extend(future.result())
            if done % 200 == 0:
                print(f"  -> Processed {done} / {len(markaz_list)} Markazs...", flush=True)

    print(f"\nPhase 1 Complete! Discovered {len(inventory)} schools.", flush=True)

    # Phase 2: fetch enrollment data
    print(f"\nPhase 2: Fetching enrollment data for ALL {len(inventory)} schools (50 threads)...", flush=True)
    done_schools, final_schools = 0, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(worker_fetch_school_data, s): s for s in inventory}
        for future in concurrent.futures.as_completed(futures):
            done_schools += 1
            final_schools.append(future.result())
            if done_schools % 500 == 0:
                print(f"  -> Fetched {done_schools} / {len(inventory)} schools...", flush=True)

    return final_schools, ts


def write_district_json_files(schools, ts):
    """Group scraped schools by district and write one JSON file per district,
    plus a master data/index.json that the frontend uses to build the
    district picker without downloading every district's full data."""

    os.makedirs(DATA_DIR, exist_ok=True)

    # Group by district_id (falls back to a slug of the name if id is blank)
    groups = {}  # key -> {"name": str, "schools": [...]}
    for s in schools:
        d_id   = s.get("district_id") or slugify(s.get("district", ""))
        d_name = s.get("district") or "Unknown"
        if d_id not in groups:
            groups[d_id] = {"name": d_name, "schools": []}
        groups[d_id]["schools"].append(s)

    index_entries = []
    used_slugs = set()

    for d_id, g in sorted(groups.items(), key=lambda kv: kv[1]["name"]):
        d_name      = g["name"]
        d_schools   = g["schools"]

        slug = slugify(d_name)
        if slug in used_slugs:
            slug = f"{slug}_{slugify(d_id)}"
        used_slugs.add(slug)

        filename = f"{slug}.json"
        filepath = os.path.join(DATA_DIR, filename)

        total_students = sum(s.get("total_school_students", 0) for s in d_schools)
        total_boys     = sum(s.get("total_school_boys",  0) for s in d_schools)
        total_girls    = sum(s.get("total_school_girls", 0) for s in d_schools)

        district_payload = {
            "district":    d_name,
            "district_id": d_id,
            "scraped_at":  ts,
            "source":      BASE,
            "summary": {
                "total_schools":  len(d_schools),
                "total_students": total_students,
                "total_boys":     total_boys,
                "total_girls":    total_girls,
            },
            "schools": d_schools,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(district_payload, f, ensure_ascii=False, indent=2)

        index_entries.append({
            "district_id":    d_id,
            "district":       d_name,
            "slug":           slug,
            "file":           f"{DATA_DIR}/{filename}",
            "total_schools":  len(d_schools),
            "total_students": total_students,
            "total_boys":     total_boys,
            "total_girls":    total_girls,
            "scraped_at":     ts,
        })

    index_payload = {
        "scraped_at": ts,
        "source": BASE,
        "summary": {
            "total_districts": len(index_entries),
            "total_schools":   sum(e["total_schools"]  for e in index_entries),
            "total_students":  sum(e["total_students"] for e in index_entries),
            "total_boys":      sum(e["total_boys"]      for e in index_entries),
            "total_girls":     sum(e["total_girls"]     for e in index_entries),
        },
        "districts": index_entries,
    }

    with open(os.path.join(DATA_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index_payload, f, ensure_ascii=False, indent=2)

    return index_payload


if __name__ == "__main__":
    print("=" * 65, flush=True)
    print("  SIS PESRP Scraper - FULL RUN (per-district JSON output)", flush=True)
    print("=" * 65, flush=True)
    start_time = time.time()

    schools, ts = scrape()
    index_payload = write_district_json_files(schools, ts)

    s = index_payload["summary"]
    with_grades = sum(
        1 for sc in schools
        if sc.get("grades") and any(g["grade_name"] != "No Data" for g in sc["grades"])
    )
    no_data = len(schools) - with_grades

    elapsed = (time.time() - start_time) / 60
    print(f"\n{'='*65}", flush=True)
    print(f"FULL RUN COMPLETE in {elapsed:.1f} minutes!", flush=True)
    print(f"{'='*65}", flush=True)
    print(f"   Districts written  : {s['total_districts']:,}", flush=True)
    print(f"   Total schools      : {s['total_schools']:,}", flush=True)
    print(f"   Total students     : {s['total_students']:,}", flush=True)
    print(f"   Schools with data  : {with_grades:,}", flush=True)
    print(f"   Schools no data    : {no_data:,}", flush=True)
    print(f"   -> {DATA_DIR}/index.json", flush=True)
    print(f"   -> {DATA_DIR}/<district_slug>.json  ({s['total_districts']} files)", flush=True)
    print(f"{'='*65}", flush=True)
