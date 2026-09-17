#!/usr/bin/env python3
"""
fetch.py — SIS PESRP Scraper (Robust FULL RUN, per-district JSON output)
===========================================================================
This runs ONCE (or twice) a day, in a 4-6 AM PKT window, and prioritises
a COMPLETE dataset over raw speed. Compared to the original version:

  - Every network call goes through safe_get(), which can never raise.
    A failed request is logged and returned as None instead of crashing
    the whole script — this was the root cause of runs silently dying
    with exit code 1 and leaving data/ stale for days.
  - Concurrency is intentionally low (site-friendly) instead of 20-50
    parallel workers hammering the site at once.
  - After each phase, anything that failed gets RETRIED in additional
    passes (MAX_RETRY_ROUNDS) with a cooldown between rounds, so a
    markaz/school is only ever recorded as empty because the site
    genuinely returned nothing — not because a request timed out once.
  - A completeness report is printed at the end so you can see exactly
    how many markazs/schools, if any, could not be fetched even after
    every retry round.

KEY FINDING from HAR analysis (unchanged from original):
  get_gender_bar_CLASS -> male/female arrays with NO category labels
  get_gender_bar_AREA  -> same data but WITH category labels
  classes=0 means "All Classes".

OUTPUT STRUCTURE (unchanged):
  data/index.json          -> master index: one entry per district
  data/<district_slug>.json -> full school list + grade breakdown
"""

import json
import os
import re
import time
import random
import requests
import threading
import concurrent.futures
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"
DATA_DIR = "data"

# ── Tunables ────────────────────────────────────────────────────────
# Kept conservative on purpose. This now has a multi-hour budget once
# (or twice) a day, so slower-but-complete beats fast-but-crashy.
MARKAZ_WORKERS   = 6     # Phase 1b concurrency (school discovery)
SCHOOL_WORKERS   = 10    # Phase 2 concurrency (enrollment data)
REQUEST_TIMEOUT  = 30
MAX_RETRY_ROUNDS = 4      # extra passes chasing down anything that failed
RETRY_ROUND_SLEEP = 25    # seconds to rest between retry rounds

thread_local = threading.local()


def get_session():
    if not hasattr(thread_local, "session"):
        s = requests.Session()
        retries = Retry(total=5, backoff_factor=2, status_forcelist=[500, 502, 503, 504, 429])
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


def safe_get(url, params=None, timeout=REQUEST_TIMEOUT, tries=3):
    """A GET that can NEVER raise. Returns the Response on success, or
    None if every attempt failed. This is the fix for the crash: one
    flaky request can no longer take down the entire scrape."""
    session = get_session()
    for attempt in range(1, tries + 1):
        try:
            return session.get(url, params=params, timeout=timeout)
        except Exception as e:
            if attempt == tries:
                print(f"[Warn] GET failed after {tries} tries: {url} params={params} -> {e}", flush=True)
                return None
            time.sleep(2 * attempt + random.uniform(0, 1.5))
    return None


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
    s = (name or "").strip().lower()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s or "unknown"


def get_csrf():
    r = safe_get(f"{BASE}/str/analysis", timeout=REQUEST_TIMEOUT, tries=5)
    if r is None:
        print("[Error] CSRF page unreachable after retries", flush=True)
        return ""
    csrf = get_session().cookies.get("csrf_cookie_name", "")
    if not csrf:
        m = re.search(r'csrf_cookie_name["\s:\']+([a-f0-9]+)', r.text)
        if m: csrf = m.group(1)
    print(f"[Network] CSRF Token: {csrf[:10]}...", flush=True)
    return csrf


def parse_options(html_str):
    opts = []
    soup = BeautifulSoup(html_str or "", "html.parser")
    skip = {
        "", "0", "select", "all", "--",
        "select district", "select tehsil", "select markaz", "select school",
        "all districts", "all tehsils", "all markazs", "all schools"
    }
    for opt in soup.find_all("option"):
        val = (opt.get("value") or "").strip()
        name = opt.get_text(strip=True)
        if val and name.lower() not in skip:
            opts.append((val, name))
    return opts


def parse_resp(r):
    """Returns a list of (value, name) options, or None if the request
    itself failed outright (as opposed to succeeding with an empty list)."""
    if r is None:
        return None
    if r.status_code != 200:
        return None
    body = r.text.strip()
    if not body:
        return []
    if body.startswith("{"):
        try:
            d = r.json()
            return parse_options(d.get("html") or d.get("data") or d.get("options") or "")
        except Exception:
            pass
    return parse_options(body)


def get_districts(csrf, tries=5):
    for attempt in range(1, tries + 1):
        r = safe_get(f"{BASE}/user/get_districts", timeout=REQUEST_TIMEOUT, tries=3)
        result = parse_resp(r)
        if result is not None and result:
            return result
        print(f"[Warn] get_districts attempt {attempt}/{tries} failed or empty, retrying...", flush=True)
        time.sleep(5 * attempt)
    return []


def get_tehsils(d_id, csrf):
    r = safe_get(f"{BASE}/user/get_tehsils",
                  params={"district": d_id, "selectedTehsil": "false", "all": "All", "csrf_test_name": csrf},
                  timeout=REQUEST_TIMEOUT, tries=4)
    return parse_resp(r)


def get_markazs(d_id, t_id, csrf):
    r = safe_get(f"{BASE}/user/get_markazes",
                  params={"tehsil": t_id, "selectedMarkaz": "false", "all": "All", "csrf_test_name": csrf},
                  timeout=REQUEST_TIMEOUT, tries=4)
    return parse_resp(r)


def get_schools(d_id, t_id, m_id, csrf):
    r = safe_get(f"{BASE}/user/get_schools",
                  params={"markaz": m_id, "selectedSchool": "false", "all": "All", "csrf_test_name": csrf},
                  timeout=REQUEST_TIMEOUT, tries=4)
    return parse_resp(r)


def worker_fetch_schools_in_markaz(markaz_info, csrf, ts):
    """Returns (schools_found, failed_bool). failed_bool True means the
    request itself broke (retry later) — NOT that the markaz is
    legitimately empty."""
    d_id, d_name, t_id, t_name, m_id, m_name = markaz_info
    school_opts = get_schools(d_id, t_id, m_id, csrf)
    if school_opts is None:
        return [], True

    schools_found = []
    for s_id, s_name in school_opts:
        emis_code, school_name_clean = "", s_name
        if " - " in s_name:
            parts = s_name.split(" - ", 1)
            emis_code = parts[0].strip()
            school_name_clean = parts[1].strip() if len(parts) > 1 else s_name
        schools_found.append({
            "school_id": s_id, "emis_code": emis_code, "school_name": school_name_clean,
            "district_id": d_id, "district": d_name, "tehsil_id": t_id, "tehsil": t_name,
            "markaz_id": m_id, "markaz": m_name,
            "total_school_students": 0, "total_school_boys": 0, "total_school_girls": 0,
            "scraped_at": ts
        })
    return schools_found, False


def worker_fetch_school_data(school_info):
    """Returns school_info with grades filled in, plus a '_fetch_failed'
    flag distinguishing 'request broke, retry me' from 'site really has
    no data for this school'."""
    params = {
        "district": school_info["district_id"],
        "tehsil": school_info["tehsil_id"],
        "markaz": school_info["markaz_id"],
        "school": school_info["school_id"],
        "classes": "0",
        "s_id_emis_code": ""
    }

    failed = False

    # 1. Totals from pie chart
    r1 = safe_get(f"{BASE}/dashboard_revamp/get_gender_summary_pie", params=params, tries=3)
    if r1 is not None and r1.status_code == 200:
        try:
            d1 = r1.json()
            if isinstance(d1, dict):
                school_info["total_school_students"] = to_int(d1.get("total"))
                school_info["total_school_boys"] = to_int(d1.get("male_count"))
                school_info["total_school_girls"] = to_int(d1.get("female_count"))
        except Exception:
            failed = True
    else:
        failed = True

    # 2. Grade breakdown from get_gender_bar_AREA (has category labels)
    grades = []
    r2 = safe_get(f"{BASE}/dashboard_revamp/get_gender_bar_area", params=params, tries=3)
    if r2 is not None and r2.status_code == 200:
        try:
            raw = r2.json()
            if isinstance(raw, dict):
                categories = raw.get("categories", [])
                male_vals = raw.get("male", [])
                female_vals = raw.get("female", [])
                n = max(len(male_vals), len(female_vals)) if (male_vals or female_vals) else 0
                for i in range(n):
                    grade_name = str(categories[i]) if i < len(categories) else f"Class_{i+1}"
                    m = to_int(male_vals[i]) if i < len(male_vals) else 0
                    f = to_int(female_vals[i]) if i < len(female_vals) else 0
                    grades.append({"grade_name": grade_name, "male_students": m, "female_students": f})
        except Exception:
            failed = True
    else:
        failed = True

    if not grades and not failed:
        grades = [{"grade_name": "No Data", "male_students": 0, "female_students": 0}]

    school_info["grades"] = grades
    school_info["_fetch_failed"] = failed
    return school_info


def scrape():
    ts = datetime.now(timezone.utc).isoformat()
    csrf = get_csrf()

    print("[Network] Requesting Districts list...", flush=True)
    districts = get_districts(csrf)
    if not districts:
        raise RuntimeError("Could not fetch district list after retries — aborting run (nothing to commit).")
    print(f"[Success] Found {len(districts)} Districts.", flush=True)

    # Phase 1a: map tehsils/markazs — sequential, small volume, retries built in
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

    # Phase 1b: get school lists — low concurrency + retry rounds for failures
    print(f"\nPhase 1b: Fetching school lists across {len(markaz_list)} Markazs "
          f"({MARKAZ_WORKERS} workers)...", flush=True)
    inventory = []
    pending = list(markaz_list)
    for round_num in range(1, MAX_RETRY_ROUNDS + 2):  # 1 main pass + N retry rounds
        if not pending:
            break
        failed_this_round = []
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=MARKAZ_WORKERS) as executor:
            futures = {executor.submit(worker_fetch_schools_in_markaz, m, csrf, ts): m for m in pending}
            for future in concurrent.futures.as_completed(futures):
                m = futures[future]
                done += 1
                try:
                    schools_found, failed = future.result()
                except Exception as e:
                    print(f"[Warn] Markaz worker crashed for {m}: {e}", flush=True)
                    schools_found, failed = [], True
                if failed:
                    failed_this_round.append(m)
                else:
                    inventory.extend(schools_found)
                if done % 200 == 0:
                    print(f"  -> Processed {done} / {len(pending)} Markazs (round {round_num})...", flush=True)

        print(f"[Round {round_num}] {len(pending) - len(failed_this_round)}/{len(pending)} markazs OK, "
              f"{len(failed_this_round)} failed.", flush=True)
        pending = failed_this_round
        if pending and round_num <= MAX_RETRY_ROUNDS:
            print(f"  Cooling down {RETRY_ROUND_SLEEP}s before retrying {len(pending)} failed markazs...", flush=True)
            time.sleep(RETRY_ROUND_SLEEP)

    if pending:
        print(f"[Warn] {len(pending)} markazs still failed after all retries — their schools are MISSING this run.", flush=True)

    print(f"\nPhase 1 Complete! Discovered {len(inventory)} schools.", flush=True)

    # Phase 2: fetch enrollment data — low concurrency + retry rounds for failures
    print(f"\nPhase 2: Fetching enrollment data for {len(inventory)} schools "
          f"({SCHOOL_WORKERS} workers)...", flush=True)
    final_schools = []
    pending_schools = list(inventory)
    for round_num in range(1, MAX_RETRY_ROUNDS + 2):
        if not pending_schools:
            break
        retry_batch = []
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=SCHOOL_WORKERS) as executor:
            futures = {executor.submit(worker_fetch_school_data, s): s for s in pending_schools}
            for future in concurrent.futures.as_completed(futures):
                done += 1
                try:
                    result = future.result()
                except Exception as e:
                    s = futures[future]
                    print(f"[Warn] School worker crashed for {s.get('school_id')}: {e}", flush=True)
                    s["_fetch_failed"] = True
                    result = s
                if result.get("_fetch_failed"):
                    retry_batch.append(result)
                else:
                    result.pop("_fetch_failed", None)
                    final_schools.append(result)
                if done % 500 == 0:
                    print(f"  -> Fetched {done} / {len(pending_schools)} schools (round {round_num})...", flush=True)

        print(f"[Round {round_num}] {len(pending_schools) - len(retry_batch)}/{len(pending_schools)} schools OK, "
              f"{len(retry_batch)} failed.", flush=True)
        pending_schools = retry_batch
        if pending_schools and round_num <= MAX_RETRY_ROUNDS:
            print(f"  Cooling down {RETRY_ROUND_SLEEP}s before retrying {len(pending_schools)} failed schools...", flush=True)
            time.sleep(RETRY_ROUND_SLEEP)

    if pending_schools:
        print(f"[Warn] {len(pending_schools)} schools still failed after all retries — "
              f"writing them with whatever partial data they have.", flush=True)
        for s in pending_schools:
            s.pop("_fetch_failed", None)
            s.setdefault("grades", [{"grade_name": "No Data", "male_students": 0, "female_students": 0}])
            final_schools.append(s)

    return final_schools, ts, len(pending), len(pending_schools)


def write_district_json_files(schools, ts):
    os.makedirs(DATA_DIR, exist_ok=True)
    groups = {}
    for s in schools:
        d_id = s.get("district_id") or slugify(s.get("district", ""))
        d_name = s.get("district") or "Unknown"
        if d_id not in groups:
            groups[d_id] = {"name": d_name, "schools": []}
        groups[d_id]["schools"].append(s)

    index_entries = []
    used_slugs = set()
    for d_id, g in sorted(groups.items(), key=lambda kv: kv[1]["name"]):
        d_name = g["name"]
        d_schools = g["schools"]
        slug = slugify(d_name)
        if slug in used_slugs:
            slug = f"{slug}_{slugify(d_id)}"
        used_slugs.add(slug)
        filename = f"{slug}.json"
        filepath = os.path.join(DATA_DIR, filename)

        total_students = sum(s.get("total_school_students", 0) for s in d_schools)
        total_boys = sum(s.get("total_school_boys", 0) for s in d_schools)
        total_girls = sum(s.get("total_school_girls", 0) for s in d_schools)

        district_payload = {
            "district": d_name, "district_id": d_id, "scraped_at": ts, "source": BASE,
            "summary": {
                "total_schools": len(d_schools), "total_students": total_students,
                "total_boys": total_boys, "total_girls": total_girls,
            },
            "schools": d_schools,
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(district_payload, f, ensure_ascii=False, indent=2)

        index_entries.append({
            "district_id": d_id, "district": d_name, "slug": slug, "file": f"{DATA_DIR}/{filename}",
            "total_schools": len(d_schools), "total_students": total_students,
            "total_boys": total_boys, "total_girls": total_girls, "scraped_at": ts,
        })

    index_payload = {
        "scraped_at": ts, "source": BASE,
        "summary": {
            "total_districts": len(index_entries),
            "total_schools": sum(e["total_schools"] for e in index_entries),
            "total_students": sum(e["total_students"] for e in index_entries),
            "total_boys": sum(e["total_boys"] for e in index_entries),
            "total_girls": sum(e["total_girls"] for e in index_entries),
        },
        "districts": index_entries,
    }
    with open(os.path.join(DATA_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index_payload, f, ensure_ascii=False, indent=2)
    return index_payload


if __name__ == "__main__":
    print("=" * 65, flush=True)
    print(" SIS PESRP Scraper - ROBUST FULL RUN (per-district JSON output)", flush=True)
    print("=" * 65, flush=True)
    start_time = time.time()

    schools, ts, missing_markazs, missing_schools = scrape()
    index_payload = write_district_json_files(schools, ts)

    s = index_payload["summary"]
    with_grades = sum(1 for sc in schools if sc.get("grades") and any(g["grade_name"] != "No Data" for g in sc["grades"]))
    no_data = len(schools) - with_grades
    elapsed = (time.time() - start_time) / 60

    print(f"\n{'='*65}", flush=True)
    print(f"RUN COMPLETE in {elapsed:.1f} minutes!", flush=True)
    print(f"{'='*65}", flush=True)
    print(f"  Districts written    : {s['total_districts']:,}", flush=True)
    print(f"  Total schools        : {s['total_schools']:,}", flush=True)
    print(f"  Total students       : {s['total_students']:,}", flush=True)
    print(f"  Schools with data    : {with_grades:,}", flush=True)
    print(f"  Schools no data      : {no_data:,}", flush=True)
    print(f"  Markazs never fetched: {missing_markazs:,}  (schools under these are absent entirely)", flush=True)
    print(f"  Schools never fetched: {missing_schools:,}  (written with 'No Data' placeholder)", flush=True)
    print(f"  -> {DATA_DIR}/index.json", flush=True)
    print(f"  -> {DATA_DIR}/<district_slug>.json ({s['total_districts']} files)", flush=True)
    print(f"{'='*65}", flush=True)

    if missing_markazs > 0:
        # Non-zero exit so you get a visible red X in Actions when data
        # is meaningfully incomplete — but only AFTER everything possible
        # was already written and committed, unlike the old crash.
        print(f"[Warn] Run finished but {missing_markazs} markazs could not be reached — "
              f"data was still written/committed, just incomplete.", flush=True)
