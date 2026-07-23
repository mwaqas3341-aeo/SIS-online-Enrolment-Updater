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

EXPECTED_TOTAL_SCHOOLS = 38150   # known-good total (as of last full count). Used
                                  # only for logging/visibility, not to abort.

# Phase 1c: how many extra times to re-check a Markaz that came back with 0
# schools, before trusting that it's genuinely empty rather than a dropped
# request under concurrent load.
MARKAZ_RECHECK_RETRIES = 6
MARKAZ_RECHECK_MAX_ROUNDS = 5     # keep looping Phase 1c while total schools
                                   # found is still below EXPECTED_TOTAL_SCHOOLS
MARKAZ_RECHECK_WAIT_SECONDS = 20  # pause between rounds so transient load clears

# Phase 2b: dedicated retry rounds for schools whose enrolment fetch failed
# (not "zero enrolment" - actually failed/None after fetch_json_with_retry's
# own retries). We wait between rounds since failures tend to be transient
# server-side hiccups that clear up after a short pause.
ENROLMENT_RETRY_MAX_ROUNDS = 3
ENROLMENT_RETRY_WAIT_SECONDS = 30

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


def worker_fetch_schools_in_markaz(markaz_info, csrf, ts, retries=4):
    d_id, d_name, t_id, t_name, m_id, m_name = markaz_info
    school_opts = get_schools(d_id, t_id, m_id, csrf, retries=retries)
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


def worker_fetch_school_data(school_info, retries=3, backoff=1.5, timeout=15):
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
    d1 = fetch_json_with_retry(f"{BASE}/dashboard_revamp/get_gender_summary_pie", params,
                                retries=retries, backoff=backoff, timeout=timeout)
    pie_ok = isinstance(d1, dict)
    if pie_ok:
        school_info["total_school_students"] = to_int(d1.get("total"))
        school_info["total_school_boys"]     = to_int(d1.get("male_count"))
        school_info["total_school_girls"]    = to_int(d1.get("female_count"))

    # 2. Grade breakdown from get_gender_bar_AREA (has category labels!)
    raw = fetch_json_with_retry(f"{BASE}/dashboard_revamp/get_gender_bar_area", params,
                                 retries=retries, backoff=backoff, timeout=timeout)
    bar_ok = isinstance(raw, dict)
    grades = []
    if bar_ok:
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

    # IMPORTANT: only accept "No Data" as a genuine result if BOTH endpoints
    # actually responded (bar_ok) and simply came back with no rows. If the
    # endpoint call itself failed (bar_ok is False), we must NOT write "No
    # Data" / leave 0s in place, because that's indistinguishable later from
    # a real zero-enrolment school. Instead we leave grades unset and flag
    # the school as failed so the retry pass below picks it up.
    if bar_ok and not grades:
        grades = [{"grade_name": "No Data", "male_students": 0, "female_students": 0}]

    fetch_ok = pie_ok and bar_ok
    if fetch_ok:
        school_info["grades"] = grades
    elif "grades" not in school_info:
        # first attempt failed and we have nothing yet - placeholder only,
        # will be overwritten by a successful retry round if one succeeds
        school_info["grades"] = grades or [{"grade_name": "No Data", "male_students": 0, "female_students": 0}]

    school_info["_fetch_ok"] = fetch_ok
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
    empty_markazs = []   # markazs that came back with 0 schools - could be
                          # genuinely empty, or a dropped request under load
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(worker_fetch_schools_in_markaz, m, csrf, ts): m for m in markaz_list}
        for future in concurrent.futures.as_completed(futures):
            done += 1
            m = futures[future]
            result = future.result()
            if result:
                inventory.extend(result)
            else:
                empty_markazs.append(m)
            if done % 200 == 0:
                print(f"  -> Processed {done} / {len(markaz_list)} Markazs...", flush=True)

    print(f"[Info] {len(empty_markazs)} Markazs came back with 0 schools on the first pass.", flush=True)

    # Phase 1c: sequentially re-check every Markaz that came back empty, with
    # more retries and no concurrent-load contention, before trusting that
    # it's genuinely a 0-school Markaz rather than a dropped request. This is
    # what was silently losing whole Markazs' worth of schools before.
    #
    # This now LOOPS: as long as the running total is still below the known
    # target (EXPECTED_TOTAL_SCHOOLS), keep re-fetching whatever Markazs are
    # still empty, rather than trusting a single recheck pass. It stops when
    # any of these happen: (a) the total reaches the target, (b) a round
    # recovers nothing new (converged - remaining Markazs are genuinely
    # empty), or (c) MARKAZ_RECHECK_MAX_ROUNDS is hit.
    still_empty_markazs = empty_markazs
    round_num = 0
    while (still_empty_markazs
           and len(inventory) < EXPECTED_TOTAL_SCHOOLS
           and round_num < MARKAZ_RECHECK_MAX_ROUNDS):
        round_num += 1
        print(f"\nPhase 1c round {round_num}/{MARKAZ_RECHECK_MAX_ROUNDS}: "
              f"total is {len(inventory)}/{EXPECTED_TOTAL_SCHOOLS} - re-checking "
              f"{len(still_empty_markazs)} empty Markazs sequentially "
              f"(retries={MARKAZ_RECHECK_RETRIES})...", flush=True)
        if round_num > 1:
            time.sleep(MARKAZ_RECHECK_WAIT_SECONDS)

        next_still_empty = []
        recovered_this_round = 0
        for i, m in enumerate(still_empty_markazs, 1):
            result = worker_fetch_schools_in_markaz(m, csrf, ts, retries=MARKAZ_RECHECK_RETRIES)
            if result:
                inventory.extend(result)
                recovered_this_round += len(result)
                print(f"  -> Recovered {len(result)} schools for {m[5]} ({m[1]}) "
                      f"that were missed on the previous pass.", flush=True)
            else:
                next_still_empty.append(m)
            if i % 50 == 0:
                print(f"  -> Rechecked {i} / {len(still_empty_markazs)} Markazs...", flush=True)

        print(f"  -> Round {round_num} recovered {recovered_this_round} schools. "
              f"Running total: {len(inventory)}/{EXPECTED_TOTAL_SCHOOLS}. "
              f"{len(next_still_empty)} Markazs still empty.", flush=True)

        still_empty_markazs = next_still_empty
        # Deliberately no early-exit on a zero-recovery round: a round-wide
        # site hiccup can affect every Markaz in the batch at once, and the
        # wait before the next round is exactly what gives it time to clear.
        # We only stop via the while-condition: target reached, no Markazs
        # left, or the round cap.

    if still_empty_markazs and len(inventory) < EXPECTED_TOTAL_SCHOOLS:
        print(f"[Warning] Still {len(still_empty_markazs)} empty Markazs and total "
              f"({len(inventory)}) remains below target ({EXPECTED_TOTAL_SCHOOLS:,}) "
              f"after {round_num} recheck round(s). These will be listed in "
              f"data/scrape_issues.json.", flush=True)

    print(f"\nPhase 1 Complete! Discovered {len(inventory)} schools.", flush=True)
    if len(inventory) < EXPECTED_TOTAL_SCHOOLS:
        print(f"[Info] {len(inventory)} is still below the known-good reference "
              f"total of {EXPECTED_TOTAL_SCHOOLS:,} even after retry rounds - this "
              f"is expected to fluctuate slightly (new/closed schools), but a large "
              f"gap may indicate a genuine site-side issue.", flush=True)

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

    # Phase 2b: dedicated retry rounds for schools whose enrolment fetch
    # failed outright (not "genuinely 0 enrolment" - the API call itself
    # never came back cleanly). This is what was previously producing rows
    # that silently show 0/0 despite the school actually having enrolment -
    # a failed fetch and a real zero looked identical before. We now retry
    # ONLY the failed subset, with lower concurrency and a wait between
    # rounds, since these failures are usually transient server load.
    failed_schools = [s for s in final_schools if not s.get("_fetch_ok", True)]
    round_num = 0
    while failed_schools and round_num < ENROLMENT_RETRY_MAX_ROUNDS:
        round_num += 1
        print(f"\n[Retry] Phase 2b round {round_num}/{ENROLMENT_RETRY_MAX_ROUNDS}: "
              f"re-fetching enrolment for {len(failed_schools)} schools that failed "
              f"the first pass...", flush=True)
        time.sleep(ENROLMENT_RETRY_WAIT_SECONDS)

        still_failed = []
        # Lower concurrency + more per-call retries than the main pass, since
        # these are the stubborn ones that already failed once.
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            futures = {
                executor.submit(worker_fetch_school_data, s, retries=5, backoff=2.0, timeout=20): s
                for s in failed_schools
            }
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if not result.get("_fetch_ok", True):
                    still_failed.append(result)

        recovered = len(failed_schools) - len(still_failed)
        print(f"  -> Recovered {recovered} / {len(failed_schools)} schools this round.", flush=True)
        failed_schools = still_failed

    if failed_schools:
        print(f"\n[Warning] {len(failed_schools)} schools STILL failed enrolment fetch "
              f"after {ENROLMENT_RETRY_MAX_ROUNDS} retry rounds. These are listed in "
              f"data/scrape_issues.json for follow-up - their enrolment numbers in "
              f"this run may be incomplete/placeholder, not confirmed zeros.", flush=True)
        _write_scrape_issues(failed_schools, still_empty_markazs)
    elif still_empty_markazs:
        _write_scrape_issues([], still_empty_markazs)

    return final_schools, ts


def _write_scrape_issues(failed_schools, still_empty_markazs):
    """Write out exactly which schools/markazs the scrape could not confirm,
    even after all retry passes, so they're visible for manual follow-up
    instead of silently blending into the data as fake zeros."""
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Entries here failed enrolment/school-list fetch even after all "
                "automated retry passes. Their numbers in the main data files "
                "may be incomplete placeholders, not confirmed real values.",
        "failed_enrolment_fetch": [
            {
                "school_id":   s.get("school_id"),
                "emis_code":   s.get("emis_code"),
                "school_name": s.get("school_name"),
                "district":    s.get("district"),
                "tehsil":      s.get("tehsil"),
                "markaz":      s.get("markaz"),
            }
            for s in failed_schools
        ],
        "still_empty_markazs": [
            {
                "district_id": m[0], "district": m[1],
                "tehsil_id":   m[2], "tehsil":   m[3],
                "markaz_id":   m[4], "markaz":   m[5],
            }
            for m in still_empty_markazs
        ],
    }
    with open(os.path.join(DATA_DIR, "scrape_issues.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[Info] Wrote data/scrape_issues.json "
          f"({len(failed_schools)} failed schools, {len(still_empty_markazs)} still-empty markazs).",
          flush=True)


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
        s.pop("_fetch_ok", None)  # internal bookkeeping only - not part of output schema
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
