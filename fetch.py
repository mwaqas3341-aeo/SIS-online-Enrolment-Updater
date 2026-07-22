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

OUTPUT STRUCTURE:

  data/index.json              -> master index: one entry per district
                                   (name, id, slug, filename, counts,
                                   scraped_at) plus global totals.
  data/<district_slug>.json    -> full school list + grade breakdown
                                   for ONE district only.

ROBUSTNESS (added after finding a ~400-school shortfall + intermittent
1-3 AM failures):

  1. Every network call (get_csrf, get_districts, get_tehsils,
     get_markazs, get_schools, and both Phase-2 enrolment calls) now
     goes through fetch_with_retry(), which retries on timeouts, HTTP
     errors, and empty bodies instead of silently returning [] / 0 on
     the first hiccup.
  2. The whole scrape() is wrapped in scrape_with_retry(), which will
     re-run the ENTIRE scrape (after a wait) if the first attempt comes
     back with a suspiciously low school count - covering the case
     where the source site itself is briefly down (e.g. maintenance
     window) rather than a single request failing.
  3. Before overwriting data/*.json, we compare the new total school
     count to the previous run's count (from the existing
     data/index.json). If the new run is drastically lower
     (< SANITY_MIN_RATIO of the previous total), we refuse to write and
     exit with an error instead of committing a bad partial scrape -
     the GitHub Action will show red, which is the intended signal.
"""

import json
import os
import re
import sys
import time
import requests
import threading
import concurrent.futures
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://sis.pesrp.edu.pk"
DATA_DIR = "data"

# --- Sanity/robustness knobs -------------------------------------------------
MIN_ACCEPTABLE_SCHOOLS = 30000   # if a scrape finds fewer than this, treat it
                                  # as a likely site outage and retry the whole
                                  # scrape rather than trusting the number.
FULL_SCRAPE_MAX_ATTEMPTS = 2
FULL_SCRAPE_RETRY_WAIT_SECONDS = 300   # 5 minutes between full-scrape retries

SANITY_MIN_RATIO = 0.95          # refuse to overwrite existing data if the
                                  # new total is below 95% of yesterday's total

# The source site appears to do maintenance roughly 1-3 AM Pakistan time
# (PKT, UTC+5). The cron schedule is already set to avoid this window, but
# GitHub Actions can occasionally delay a scheduled run under load - this
# is a belt-and-suspenders check so the script refuses to scrape if it
# somehow still starts inside the window, rather than burning 20-30 minutes
# hitting a site that's likely down anyway.
PKT = timezone(timedelta(hours=5))
EXCLUDED_WINDOW_START_HOUR_PKT = 1   # 1:00 AM PKT
EXCLUDED_WINDOW_END_HOUR_PKT = 3     # 3:00 AM PKT (exclusive)


def in_excluded_window():
    now_pkt = datetime.now(PKT)
    return EXCLUDED_WINDOW_START_HOUR_PKT <= now_pkt.hour < EXCLUDED_WINDOW_END_HOUR_PKT


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


def fetch_with_retry(url, params=None, retries=4, backoff=2, timeout=15):
    """GET a URL with retries on timeouts, connection errors, and non-200s.

    Unlike the session-level urllib3 Retry (which only covers certain HTTP
    status codes), this also catches request-level exceptions (timeouts,
    connection resets) that were previously uncaught and would crash the
    whole script the moment they hit get_csrf/get_districts/get_tehsils/
    get_markazs.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            r = get_session().get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            last_exc = Exception(f"HTTP {r.status_code} from {url}")
        except Exception as e:
            last_exc = e
        if attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
    raise last_exc if last_exc else Exception(f"fetch_with_retry: unknown failure for {url}")


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
        r = fetch_with_retry(f"{BASE}/str/analysis", retries=4, backoff=2)
        csrf = session.cookies.get("csrf_cookie_name", "")
        if not csrf:
            m = re.search(r'csrf_cookie_name["\s:\']+([a-f0-9]+)', r.text)
            if m: csrf = m.group(1)
        print(f"[Network] CSRF Token: {csrf[:10]}...", flush=True)
        return csrf
    except Exception as e:
        print(f"[Error] CSRF failed after retries: {e}", flush=True)
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


def get_districts_list():
    try:
        r = fetch_with_retry(f"{BASE}/user/get_districts", retries=4, backoff=2)
        return parse_resp(r)
    except Exception as e:
        print(f"[FATAL] Could not fetch districts list after retries: {e}", flush=True)
        return []


def get_tehsils(d_id, csrf):
    try:
        r = fetch_with_retry(
            f"{BASE}/user/get_tehsils",
            params={"district": d_id, "selectedTehsil": "false", "all": "All", "csrf_test_name": csrf},
            retries=4, backoff=2
        )
        return parse_resp(r)
    except Exception as e:
        print(f"[Warning] get_tehsils failed for district {d_id} after retries: {e}", flush=True)
        return []


def get_markazs(d_id, t_id, csrf):
    try:
        r = fetch_with_retry(
            f"{BASE}/user/get_markazes",
            params={"tehsil": t_id, "selectedMarkaz": "false", "all": "All", "csrf_test_name": csrf},
            retries=4, backoff=2
        )
        return parse_resp(r)
    except Exception as e:
        print(f"[Warning] get_markazs failed for district {d_id} / tehsil {t_id} after retries: {e}", flush=True)
        return []


def get_schools(d_id, t_id, m_id, csrf, retries=4):
    """Fetch schools for one markaz, retrying on empty/failed responses.

    An empty result can mean either "this markaz genuinely has 0 schools"
    or "the request silently failed under concurrent load" (the same
    issue that caused missing markazs back in Phase 1a). We retry a few
    times with a short backoff before trusting an empty result as real.
    """
    for attempt in range(retries):
        try:
            r = get_session().get(
                f"{BASE}/user/get_schools",
                params={"markaz": m_id, "selectedSchool": "false", "all": "All", "csrf_test_name": csrf},
                timeout=15
            )
            opts = parse_resp(r)
        except Exception:
            opts = []
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


def fetch_json_with_retry(url, params, retries=3, backoff=1.5, timeout=15):
    """Like fetch_with_retry, but also validates the body parses as JSON
    before accepting it - a 200 with an empty/HTML error body would
    otherwise be silently treated as valid and produce 0/0 enrolment."""
    last_exc = None
    for attempt in range(retries):
        try:
            r = get_session().get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                return data
            last_exc = Exception(f"HTTP {r.status_code} from {url}")
        except Exception as e:
            last_exc = e
        if attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
    return None  # caller treats None as "no data available after retries"


def worker_fetch_school_data(school_info):
    # params: classes=0 means "All Classes" (confirmed from HAR)
    params = {
        "district":       school_info["district_id"],
        "tehsil":         school_info["tehsil_id"],
        "markaz":         school_info["markaz_id"],
        "school":         school_info["school_id"],
        "classes":        "0",        # "0" = All Classes
        "s_id_emis_code": ""
    }

    # 1. Totals from pie chart (now retried instead of single-shot try/except)
    d1 = fetch_json_with_retry(f"{BASE}/dashboard_revamp/get_gender_summary_pie", params)
    if isinstance(d1, dict):
        school_info["total_school_students"] = to_int(d1.get("total"))
        school_info["total_school_boys"]     = to_int(d1.get("male_count"))
        school_info["total_school_girls"]    = to_int(d1.get("female_count"))

    # 2. Grade breakdown from get_gender_bar_AREA (has category labels!)
    grades = []
    raw = fetch_json_with_retry(f"{BASE}/dashboard_revamp/get_gender_bar_area", params)
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

    if not grades:
        grades = [{"grade_name": "No Data", "male_students": 0, "female_students": 0}]

    school_info["grades"] = grades
    return school_info


def scrape():
    ts   = datetime.now(timezone.utc).isoformat()
    csrf = get_csrf()

    print("[Network] Requesting Districts list...", flush=True)
    districts = get_districts_list()
    print(f"[Success] Found {len(districts)} Districts.", flush=True)

    if not districts:
        print("[FATAL] No districts found - aborting this attempt.", flush=True)
        return [], ts

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
    # Lowered from 20 -> 8 workers, plus retries in get_schools(). High-markaz
    # districts (GUJRAT, GUJRANWALA, MIANWALI, HAFIZABAD, NANKANA SAHIB) were
    # losing 10-20% of schools to silent failures under concurrent load.
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

    # Phase 2: fetch enrollment data (now with retries inside worker_fetch_school_data)
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


def scrape_with_retry():
    """Run scrape() up to FULL_SCRAPE_MAX_ATTEMPTS times if the result looks
    suspiciously small (e.g. the source site was down/mid-maintenance,
    which matches the observed intermittent 1-3 AM failures)."""
    schools, ts = [], datetime.now(timezone.utc).isoformat()

    for attempt in range(1, FULL_SCRAPE_MAX_ATTEMPTS + 1):
        print(f"\n{'#'*65}", flush=True)
        print(f"# Full scrape attempt {attempt}/{FULL_SCRAPE_MAX_ATTEMPTS}", flush=True)
        print(f"{'#'*65}", flush=True)

        schools, ts = scrape()

        if len(schools) >= MIN_ACCEPTABLE_SCHOOLS:
            return schools, ts

        print(f"[Warning] Attempt {attempt} found only {len(schools)} schools "
              f"(below the {MIN_ACCEPTABLE_SCHOOLS:,} sanity floor) - the source "
              f"site may be down or mid-maintenance.", flush=True)

        if attempt < FULL_SCRAPE_MAX_ATTEMPTS:
            print(f"Waiting {FULL_SCRAPE_RETRY_WAIT_SECONDS}s before retrying the "
                  f"entire scrape...", flush=True)
            time.sleep(FULL_SCRAPE_RETRY_WAIT_SECONDS)

    return schools, ts  # last attempt's result, even if still below the floor


def build_payloads(schools, ts):
    """Group scraped schools by district and build the district JSON payloads
    plus the master index payload, WITHOUT writing anything to disk yet -
    so the caller can sanity-check the totals before committing them."""

    groups = {}  # key -> {"name": str, "schools": [...]}
    for s in schools:
        d_id   = s.get("district_id") or slugify(s.get("district", ""))
        d_name = s.get("district") or "Unknown"
        if d_id not in groups:
            groups[d_id] = {"name": d_name, "schools": []}
        groups[d_id]["schools"].append(s)

    index_entries = []
    district_payloads = {}  # filename -> payload
    used_slugs = set()

    for d_id, g in sorted(groups.items(), key=lambda kv: kv[1]["name"]):
        d_name    = g["name"]
        d_schools = g["schools"]

        slug = slugify(d_name)
        if slug in used_slugs:
            slug = f"{slug}_{slugify(d_id)}"
        used_slugs.add(slug)

        filename = f"{slug}.json"

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
        district_payloads[filename] = district_payload

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

    return index_payload, district_payloads


def load_previous_total():
    """Read the previously committed data/index.json (if any) to get
    yesterday's total school count, for the sanity check before overwrite."""
    path = os.path.join(DATA_DIR, "index.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            prev = json.load(f)
        return prev.get("summary", {}).get("total_schools")
    except Exception:
        return None


def write_payloads(index_payload, district_payloads):
    os.makedirs(DATA_DIR, exist_ok=True)
    for filename, payload in district_payloads.items():
        filepath = os.path.join(DATA_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DATA_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index_payload, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print("=" * 65, flush=True)
    print("  SIS PESRP Scraper - FULL RUN (per-district JSON output)", flush=True)
    print("=" * 65, flush=True)

    if in_excluded_window():
        now_pkt = datetime.now(PKT)
        print(f"[Skip] Current time is {now_pkt.strftime('%H:%M')} PKT, inside the "
              f"excluded {EXCLUDED_WINDOW_START_HOUR_PKT}-{EXCLUDED_WINDOW_END_HOUR_PKT} AM "
              f"PKT maintenance window. Skipping this run entirely - the next "
              f"scheduled run will pick it up. This is not a failure.", flush=True)
        sys.exit(0)

    start_time = time.time()

    prev_total = load_previous_total()
    if prev_total:
        print(f"[Info] Previous run had {prev_total:,} total schools.", flush=True)

    schools, ts = scrape_with_retry()
    index_payload, district_payloads = build_payloads(schools, ts)
    new_total = index_payload["summary"]["total_schools"]

    # --- Sanity check: refuse to overwrite good data with a bad partial run ---
    if prev_total and new_total < prev_total * SANITY_MIN_RATIO:
        print(f"\n{'!'*65}", flush=True)
        print(f"[ABORT] New total ({new_total:,}) is below "
              f"{SANITY_MIN_RATIO*100:.0f}% of the previous total ({prev_total:,}).", flush=True)
        print("Refusing to overwrite data/*.json with what looks like a bad "
              "partial scrape. No files were written this run.", flush=True)
        print(f"{'!'*65}", flush=True)
        sys.exit(1)

    write_payloads(index_payload, district_payloads)

    with_grades = sum(
        1 for sc in schools
        if sc.get("grades") and any(g["grade_name"] != "No Data" for g in sc["grades"])
    )
    no_data = len(schools) - with_grades

    elapsed = (time.time() - start_time) / 60
    print(f"\n{'='*65}", flush=True)
    print(f"FULL RUN COMPLETE in {elapsed:.1f} minutes!", flush=True)
    print(f"{'='*65}", flush=True)
    print(f"   Districts written  : {index_payload['summary']['total_districts']:,}", flush=True)
    print(f"   Total schools      : {new_total:,}", flush=True)
    print(f"   Total students     : {index_payload['summary']['total_students']:,}", flush=True)
    print(f"   Schools with data  : {with_grades:,}", flush=True)
    print(f"   Schools no data    : {no_data:,}", flush=True)
    print(f"   -> {DATA_DIR}/index.json", flush=True)
    print(f"   -> {DATA_DIR}/<district_slug>.json  ({index_payload['summary']['total_districts']} files)", flush=True)
    print(f"{'='*65}", flush=True)
