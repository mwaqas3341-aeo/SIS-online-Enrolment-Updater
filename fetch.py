
"""
fetch.py  —  SIS PESRP School-wise Enrollment Scraper
Strategy:
  1. Open /str/analysis with Playwright
  2. Intercept ALL network responses — capture AJAX JSON automatically
  3. Drive the dropdowns: Province → District → Tehsil → School
  4. Collect per-school enrollment + teacher data
  5. Write data.json for GitHub Pages front-end
"""

import json, time, re
from datetime import datetime, timezone
from collections import defaultdict
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL   = "https://sis.pesrp.edu.pk"
STATS_URL  = f"{BASE_URL}/str/analysis"
TIMEOUT    = 30_000   # ms

# ─────────────────────────────────────────────────────────────────────────────
# Network interceptor — captures every JSON response automatically
# ─────────────────────────────────────────────────────────────────────────────
captured_requests = []

def handle_response(response):
    """Called for every network response. Saves JSON ones."""
    try:
        ct = response.headers.get("content-type", "")
        if "json" in ct or "javascript" in ct:
            url  = response.url
            body = response.text()
            if len(body) > 10 and body.strip().startswith(("[", "{")):
                captured_requests.append({
                    "url":    url,
                    "status": response.status,
                    "body":   body[:50_000],   # cap at 50 KB
                })
    except Exception:
        pass   # ignore binary / empty


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_options(page, selector):
    """Return list of (value, label) for a <select> element."""
    opts = []
    for el in page.query_selector_all(f"{selector} option"):
        val = el.get_attribute("value") or ""
        txt = (el.inner_text() or "").strip()
        if val and txt and txt.lower() not in ("select", "all", "--", "select district",
                                                "select tehsil", "select markaz", "select school"):
            opts.append((val, txt))
    return opts

def wait_for_data(page, ms=2500):
    """Short pause for AJAX to settle after a dropdown change."""
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
    time.sleep(1.5)

def select_and_wait(page, selector, value):
    try:
        page.select_option(selector, value=value)
        wait_for_data(page)
        return True
    except Exception:
        return False

def try_parse(text):
    try:
        return json.loads(text)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Main scraper
# ─────────────────────────────────────────────────────────────────────────────
def scrape():
    output = {
        "scraped_at":      datetime.now(timezone.utc).isoformat(),
        "source":          BASE_URL,
        "discovered_apis": [],          # auto-discovered AJAX endpoints
        "districts":       [],          # district list
        "schools":         [],          # flat list of schools with enrollment
        "summary": {
            "total_schools":  0,
            "total_students": 0,
            "total_boys":     0,
            "total_girls":    0,
            "total_teachers": 0,
        },
        "error": None,
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124 Safari/537.36"
            )
        )
        page = ctx.new_page()

        # ── Attach network interceptor ───────────────────────────────────────
        page.on("response", handle_response)

        # ── Step 1: Load /str/analysis ───────────────────────────────────────
        print("Opening /str/analysis …")
        try:
            page.goto(STATS_URL, wait_until="networkidle", timeout=60_000)
        except PWTimeout:
            output["error"] = "Timeout loading /str/analysis"
            browser.close()
            return output

        wait_for_data(page, 5000)

        # ── Step 2: Discover dropdowns ───────────────────────────────────────
        # Common selector patterns in SIS-type CodeIgniter apps
        district_selectors = [
            "select[name='district']", "select#district", "select.district",
            "select[name='district_id']", "#district_id", "#sel_district",
        ]
        tehsil_selectors = [
            "select[name='tehsil']", "select#tehsil", "select.tehsil",
            "select[name='tehsil_id']", "#tehsil_id", "#sel_tehsil",
        ]
        school_selectors = [
            "select[name='school']", "select#school", "select.school",
            "select[name='school_id']", "#school_id", "#sel_school",
        ]

        def find_sel(candidates):
            for s in candidates:
                if page.query_selector(s):
                    return s
            # fallback: inspect all selects
            for sel in page.query_selector_all("select"):
                nm = (sel.get_attribute("name") or sel.get_attribute("id") or "").lower()
                if any(k in nm for k in ["district"]):
                    return f"select[name='{sel.get_attribute('name')}']" if sel.get_attribute('name') else f"#{sel.get_attribute('id')}"
            return None

        dist_sel = find_sel(district_selectors)

        if not dist_sel:
            # No dropdown found — save whatever AJAX was captured on load
            output["error"] = "No district dropdown found. Check captured_requests for raw API data."
            _save_captured(output)
            browser.close()
            return output

        # ── Step 3: Get district list ────────────────────────────────────────
        districts = get_options(page, dist_sel)
        print(f"Found {len(districts)} districts")
        output["districts"] = [{"id": v, "name": n} for v, n in districts]

        teh_sel    = find_sel(tehsil_selectors)
        school_sel = find_sel(school_selectors)

        # ── Step 4: Iterate districts → tehsils → schools ────────────────────
        all_schools = []

        for d_val, d_name in districts:
            print(f"  District: {d_name}")
            if not select_and_wait(page, dist_sel, d_val):
                continue

            # Refresh tehsil options after selecting district
            tehsils = get_options(page, teh_sel) if teh_sel else []

            if not tehsils:
                # Try to read school-level data directly
                schools = _extract_school_table(page, d_name, "", "")
                all_schools.extend(schools)
                continue

            for t_val, t_name in tehsils:
                print(f"    Tehsil: {t_name}")
                if teh_sel:
                    select_and_wait(page, teh_sel, t_val)

                # School list within tehsil
                if school_sel:
                    school_opts = get_options(page, school_sel)
                    for s_val, s_name in school_opts:
                        select_and_wait(page, school_sel, s_val)
                        school_data = _extract_school_stats(page)
                        all_schools.append({
                            "district":     d_name,
                            "tehsil":       t_name,
                            "school_id":    s_val,
                            "school_name":  s_name,
                            **school_data,
                        })
                else:
                    # No school dropdown — read aggregate table for this tehsil
                    rows = _extract_school_table(page, d_name, t_name, "")
                    all_schools.extend(rows)

        output["schools"] = all_schools

        # ── Step 5: Compute summary totals ───────────────────────────────────
        for s in all_schools:
            output["summary"]["total_schools"]  += 1
            output["summary"]["total_students"] += _int(s.get("total_students", 0))
            output["summary"]["total_boys"]     += _int(s.get("boys", 0))
            output["summary"]["total_girls"]    += _int(s.get("girls", 0))
            output["summary"]["total_teachers"] += _int(s.get("teachers", 0))

        # ── Step 6: Save auto-discovered AJAX APIs ───────────────────────────
        _save_captured(output)

        browser.close()

    return output


# ─────────────────────────────────────────────────────────────────────────────
# Extract stats from the currently displayed page
# ─────────────────────────────────────────────────────────────────────────────
def _extract_school_stats(page):
    """
    After selecting a school in the dropdown, read the displayed stats.
    Tries multiple common patterns.
    """
    stats = {"total_students": 0, "boys": 0, "girls": 0, "teachers": 0}
    body  = page.inner_text("body")

    # Pattern: look for labeled numbers  e.g. "Boys: 123"  "Girls: 456"
    for label, key in [
        (r"boys\s*:?\s*([\d,]+)",        "boys"),
        (r"girls\s*:?\s*([\d,]+)",       "girls"),
        (r"total\s+students?\s*:?\s*([\d,]+)", "total_students"),
        (r"teachers?\s*:?\s*([\d,]+)",   "teachers"),
        (r"enrollment\s*:?\s*([\d,]+)",  "total_students"),
    ]:
        m = re.search(label, body, re.IGNORECASE)
        if m:
            stats[key] = _int(m.group(1))

    # If we got boys+girls but not total, compute it
    if stats["total_students"] == 0 and (stats["boys"] or stats["girls"]):
        stats["total_students"] = stats["boys"] + stats["girls"]

    # Try table cells too
    rows = page.query_selector_all("table tbody tr")
    for row in rows:
        cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
        if len(cells) >= 3:
            nums = [_int(c) for c in cells if re.match(r"^[\d,]+$", c.replace(",",""))]
            if nums:
                stats["total_students"] = max(stats["total_students"], sum(nums[:2]))

    return stats


def _extract_school_table(page, district, tehsil, markaz):
    """
    Reads any visible table as rows of school data.
    Returns list of dicts.
    """
    schools = []
    for tbl in page.query_selector_all("table"):
        headers = [clean(th.inner_text()) for th in tbl.query_selector_all("th")]
        for tr in tbl.query_selector_all("tbody tr"):
            cells = [clean(td.inner_text()) for td in tr.query_selector_all("td")]
            if not cells:
                continue
            row = {"district": district, "tehsil": tehsil, "markaz": markaz}
            if headers:
                for i, h in enumerate(headers):
                    if i < len(cells):
                        row[h.lower().replace(" ", "_")] = cells[i]
            else:
                # No headers — try to infer
                row["school_name"]    = cells[0] if len(cells) > 0 else ""
                row["total_students"] = _int(cells[1]) if len(cells) > 1 else 0
                row["boys"]           = _int(cells[2]) if len(cells) > 2 else 0
                row["girls"]          = _int(cells[3]) if len(cells) > 3 else 0
                row["teachers"]       = _int(cells[4]) if len(cells) > 4 else 0
            schools.append(row)
    return schools


def _save_captured(output):
    """Save discovered AJAX endpoints and their parsed responses."""
    seen_urls = set()
    apis = []
    for req in captured_requests:
        url = req["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        parsed = try_parse(req["body"])
        apis.append({
            "url":     url,
            "status":  req["status"],
            "sample":  parsed if isinstance(parsed, (dict, list)) else req["body"][:500],
        })

        # If the AJAX response contains school/enrollment data, merge it
        if isinstance(parsed, list) and len(parsed) > 0:
            first = parsed[0]
            if isinstance(first, dict):
                keys = {k.lower() for k in first.keys()}
                if any(k in keys for k in ["enrollment", "students", "boys", "girls",
                                            "school", "emis", "school_name"]):
                    print(f"  ✓ Found school-data API: {url}  ({len(parsed)} rows)")
                    # Merge into schools if not already populated
                    if not output["schools"]:
                        output["schools"] = parsed

    output["discovered_apis"] = apis[:30]   # keep up to 30


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────
def _int(v):
    try:
        return int(str(v).replace(",", "").strip())
    except Exception:
        return 0

def clean(text):
    return re.sub(r'\s+', ' ', (text or "")).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== SIS PESRP School Enrollment Scraper ===")
    data = scrape()

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    s = data["summary"]
    print(f"\n✓ data.json written")
    print(f"  Schools     : {s['total_schools']:,}")
    print(f"  Students    : {s['total_students']:,}")
    print(f"  Boys        : {s['total_boys']:,}")
    print(f"  Girls       : {s['total_girls']:,}")
    print(f"  Teachers    : {s['total_teachers']:,}")
    print(f"  AJAX APIs   : {len(data['discovered_apis'])}")

    if data.get("error"):
        print(f"\n⚠ {data['error']}")

    # Print discovered API endpoints — useful for debugging
    if data["discovered_apis"]:
        print("\n--- Discovered AJAX Endpoints ---")
        for api in data["discovered_apis"][:10]:
            print(f"  {api['status']}  {api['url']}")
