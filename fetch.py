"""
fetch.py — SIS PESRP Scraper (v2)
Strategy:
  1. Load /str/analysis and extract all <script> src URLs
  2. Search JS files for API endpoint patterns
  3. Also try common CodeIgniter endpoint patterns directly
  4. Use requests to call each endpoint and collect school data
  5. Fallback: Playwright clicks every dropdown option and captures responses
"""

import json, csv, re, time, requests
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE    = "https://sis.pesrp.edu.pk"
URL     = f"{BASE}/str/analysis"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": URL,
}

# ── Captured AJAX responses (Playwright) ─────────────────────────────────────
ajax = []

def on_response(resp):
    try:
        url  = resp.url
        ct   = resp.headers.get("content-type","")
        body = resp.text()
        if body and body.strip()[:1] in ("[","{"):
            ajax.append({"url": url, "status": resp.status, "body": body[:50000]})
            print(f"  [AJAX] {resp.status} {url[:80]}")
    except Exception:
        pass

# ── Step 1: Extract JS URLs and find API endpoints ────────────────────────────
def find_api_endpoints(html, js_texts):
    """Search HTML and JS source for AJAX endpoint patterns."""
    endpoints = set()
    combined  = html + "\n" + "\n".join(js_texts)

    # Common patterns in CodeIgniter / Laravel SIS apps
    patterns = [
        r"""(?:url|action)\s*[:=]\s*['"`]([^'"`]+(?:str|stats|api|get|fetch|load|school|district|tehsil|enroll)[^'"`]*)['"`]""",
        r"""(?:ajax|fetch|post|get)\s*\(\s*['"`]([^'"`]+)['"`]""",
        r"""['"`](/[a-z_/]+(?:school|district|tehsil|enroll|stats|str)[a-z_/]*)['"`]""",
        r"""url\s*:\s*site_url\s*\(\s*['"`]([^'"`]+)['"`]\s*\)""",
        r"""base_url\s*\+\s*['"`]([^'"`]+)['"`]""",
    ]

    for pat in patterns:
        for m in re.finditer(pat, combined, re.IGNORECASE):
            ep = m.group(1).strip()
            if ep.startswith("/") or ep.startswith("http"):
                if not ep.startswith("http"):
                    ep = BASE + ep
                endpoints.add(ep)

    # Also try known CodeIgniter URL patterns for this type of system
    guesses = [
        "/str/get_districts",
        "/str/get_tehsils",
        "/str/get_markazs",
        "/str/get_schools",
        "/str/get_school_data",
        "/str/get_enrollment",
        "/str/school_list",
        "/str/district_data",
        "/str/stats",
        "/stats/get_district",
        "/stats/get_school",
        "/api/schools",
        "/api/enrollment",
        "/api/districts",
        "/str/analysis/get_data",
        "/str/analysis/schools",
        "/str/analysis/district",
        "/home/get_stats",
        "/home/stats",
    ]
    for g in guesses:
        endpoints.add(BASE + g)

    return list(endpoints)

# ── Step 2: Try each endpoint with GET and POST ───────────────────────────────
def probe_endpoints(endpoints):
    """Call each endpoint and return ones that return JSON school data."""
    found = []
    sess  = requests.Session()
    sess.headers.update(HEADERS)

    for ep in endpoints:
        for method in ("GET", "POST"):
            try:
                if method == "GET":
                    r = sess.get(ep, timeout=10)
                else:
                    r = sess.post(ep, data={
                        "district_id":"", "tehsil_id":"", "markaz_id":"",
                        "district":"",    "tehsil":"",    "markaz":"",
                    }, timeout=10)

                if r.status_code == 200 and r.text.strip()[:1] in ("[", "{"):
                    data = r.json()
                    rows = parse_json(data)
                    if rows:
                        print(f"  ✓ {method} {ep} → {len(rows)} rows")
                        found.append({"url": ep, "method": method, "rows": rows})
                    else:
                        # Might be a list of districts/tehsils — save for drilling
                        if isinstance(data, list) and len(data) > 0:
                            print(f"  ~ {method} {ep} → list of {len(data)} items (not school data)")
                            found.append({"url": ep, "method": method, "rows": [], "raw": data})
            except Exception:
                pass

    return found

# ── Step 3: Drill district → tehsil → school ─────────────────────────────────
def drill(districts_url, method, districts_raw, sess):
    """Given a district list, drill down to get per-school data."""
    all_rows = []

    for d in districts_raw[:36]:  # Punjab has 36 districts
        d_id  = d.get("district_id") or d.get("id") or d.get("value") or ""
        d_nm  = d.get("district_name") or d.get("name") or d.get("text") or str(d_id)
        if not d_id:
            continue

        print(f"  District: {d_nm}")
        # Try to get tehsils for this district
        for tep in ["/str/get_tehsils", "/str/get_tehsil", "/api/tehsils"]:
            try:
                r = sess.post(BASE+tep,
                    data={"district_id": d_id, "district": d_id}, timeout=8)
                if r.status_code==200 and r.text.strip()[:1] in ("[","{"):
                    tehsils = r.json()
                    if isinstance(tehsils, list) and tehsils:
                        all_rows.extend(drill_tehsils(tehsils, d_id, d_nm, sess))
                        break
            except Exception:
                pass
        else:
            # No tehsil endpoint — try school endpoint directly
            all_rows.extend(try_school_endpoint(d_id, d_nm, "", "", sess))

    return all_rows

def drill_tehsils(tehsils, d_id, d_nm, sess):
    rows = []
    for t in tehsils:
        t_id = t.get("tehsil_id") or t.get("id") or t.get("value") or ""
        t_nm = t.get("tehsil_name") or t.get("name") or t.get("text") or str(t_id)
        if not t_id:
            continue
        rows.extend(try_school_endpoint(d_id, d_nm, t_id, t_nm, sess))
    return rows

def try_school_endpoint(d_id, d_nm, t_id, t_nm, sess):
    rows = []
    for sep in ["/str/get_schools","/str/school_list","/str/get_school_data",
                "/api/schools","/str/analysis/schools"]:
        try:
            r = sess.post(BASE+sep, data={
                "district_id": d_id, "district": d_id,
                "tehsil_id":   t_id, "tehsil":   t_id,
            }, timeout=10)
            if r.status_code==200 and r.text.strip()[:1] in ("[","{"):
                parsed = parse_json(r.json(), d_nm, t_nm)
                if parsed:
                    print(f"    {t_nm}: {len(parsed)} schools from {sep}")
                    rows.extend(parsed)
                    break
        except Exception:
            pass
    return rows

# ── Parse school JSON ─────────────────────────────────────────────────────────
NAME_K  = ["school_name","name","school","sch_name","school_title","sname"]
TOT_K   = ["total","total_students","enrollment","students","enrolled","total_enrol","tot_enrol"]
BOYS_K  = ["boys","male","male_enrollment","boy_count","male_count","boys_enrol"]
GIRLS_K = ["girls","female","female_enrollment","girl_count","female_count","girls_enrol"]
TCH_K   = ["teachers","teacher_count","allocated_teachers","staff","tch_count"]
DIST_K  = ["district","district_name","dist_name","dname"]
TEH_K   = ["tehsil","tehsil_name","teh_name","tname"]
MRK_K   = ["markaz","markaz_name","mrk_name"]
ID_K    = ["school_id","id","emis","emis_code","school_code","scode"]

def gf(row, keys):
    rl = {k.lower(): v for k,v in row.items()}
    for k in keys:
        if k in rl: return rl[k]
    return ""

def num(v):
    try: return int(re.sub(r"[^\d]","",str(v or 0)) or 0)
    except: return 0

def clean(t): return re.sub(r"\s+"," ",(t or "")).strip()

def parse_json(data, dist="", teh=""):
    rows = []
    if isinstance(data, dict):
        for key in ("data","result","schools","rows","records","items","list"):
            if key in data and isinstance(data[key], list):
                data = data[key]; break
        else:
            return rows
    if not isinstance(data, list): return rows

    for item in data:
        if not isinstance(item, dict): continue
        keys = {k.lower() for k in item}
        # Must look like school data
        has_school = any(k in keys for k in ["school_name","school","emis","sch_name","sname"])
        has_data   = any(k in keys for k in ["enrollment","students","boys","girls","total","tot_enrol"])
        if not (has_school or has_data): continue

        b = num(gf(item, BOYS_K))
        g = num(gf(item, GIRLS_K))
        t = num(gf(item, TOT_K)) or b + g
        rows.append({
            "school_id":      str(gf(item, ID_K) or ""),
            "school_name":    clean(gf(item, NAME_K)) or "Unknown",
            "district":       clean(gf(item, DIST_K)) or dist,
            "tehsil":         clean(gf(item, TEH_K))  or teh,
            "markaz":         clean(gf(item, MRK_K)),
            "total_students": t,
            "boys":           b,
            "girls":          g,
            "teachers":       num(gf(item, TCH_K)),
        })
    return rows

# ── Playwright fallback: click every dropdown option ─────────────────────────
def playwright_fallback(ts):
    print("\n--- Playwright fallback: driving dropdowns ---")
    rows = []

    with sync_playwright() as pw:
        br  = pw.chromium.launch(headless=True)
        ctx = br.new_context(user_agent=HEADERS["User-Agent"])
        pg  = ctx.new_page()
        pg.on("response", on_response)

        try:
            pg.goto(URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"  Page load error: {e}")
            br.close()
            return rows

        time.sleep(5)

        # Print page title and all select elements found
        title = pg.title()
        print(f"  Page title: {title}")

        selects = pg.query_selector_all("select")
        print(f"  Found {len(selects)} <select> elements")
        for sel in selects:
            nm = sel.get_attribute("name") or sel.get_attribute("id") or "?"
            opts = sel.query_selector_all("option")
            print(f"    select[{nm}]: {len(opts)} options")

        # Try clicking any buttons or links that might load data
        for btn_text in ["Search","Load","Get Data","Show","Filter","Submit","Go"]:
            try:
                btn = pg.get_by_text(btn_text, exact=False).first
                if btn:
                    btn.click()
                    time.sleep(2)
                    print(f"  Clicked button: {btn_text}")
            except Exception:
                pass

        # Get page source and look for inline JSON
        html = pg.content()
        json_blobs = re.findall(r'var\s+\w+\s*=\s*(\[{.*?}\])', html, re.DOTALL)
        for blob in json_blobs[:5]:
            try:
                parsed = json.loads(blob)
                r = parse_json(parsed)
                if r:
                    print(f"  Found {len(r)} rows in inline JS variable")
                    rows.extend(r)
            except Exception:
                pass

        # Extract all script src URLs
        scripts = [s.get_attribute("src") for s in pg.query_selector_all("script[src]")]
        scripts = [s for s in scripts if s and "pesrp" in s.lower() or "sis" in s.lower()
                   or "/assets/" in s or "/static/" in s or "/js/" in s]
        print(f"  Script files: {scripts[:5]}")

        br.close()

    # Check AJAX captures
    for entry in ajax:
        try:
            data   = json.loads(entry["body"])
            parsed = parse_json(data)
            if parsed:
                print(f"  AJAX gave {len(parsed)} rows: {entry['url'][:70]}")
                rows.extend(parsed)
        except Exception:
            pass

    return rows, html, scripts

# ── Save CSV ──────────────────────────────────────────────────────────────────
FIELDS = ["school_id","school_name","district","tehsil","markaz",
          "total_students","boys","girls","teachers"]

def save_csv(rows, ts):
    with open("schools.csv","w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS+["scraped_at"], extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r["scraped_at"] = ts
            w.writerow(r)
    print(f"✓ schools.csv ({len(rows)} rows)")

def save_json(rows, ts, endpoints_tried):
    tot = sum(r.get("total_students",0) for r in rows)
    out = {
        "scraped_at": ts,
        "source":     BASE,
        "summary": {
            "total_schools":  len(rows),
            "total_students": tot,
            "total_boys":     sum(r.get("boys",0)     for r in rows),
            "total_girls":    sum(r.get("girls",0)    for r in rows),
            "total_teachers": sum(r.get("teachers",0) for r in rows),
        },
        "schools": rows,
        "ajax_endpoints": [{"url":e["url"],"status":e["status"]} for e in ajax[:30]],
        "endpoints_tried": endpoints_tried[:20],
    }
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"✓ data.json ({len(rows)} schools | {tot:,} students)")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ts = datetime.now(timezone.utc).isoformat()
    print("="*55)
    print("  SIS PESRP Scraper v2")
    print("="*55)

    # Step 1: Playwright to get page HTML + capture AJAX + find scripts
    print("\n[1] Loading page and discovering structure …")
    all_rows, html, scripts = playwright_fallback(ts)

    sess = requests.Session()
    sess.headers.update(HEADERS)

    # Step 2: Fetch JS files and extract endpoint URLs
    print("\n[2] Scanning JS files for API endpoints …")
    js_texts = []
    for src in scripts[:10]:
        try:
            if not src.startswith("http"):
                src = BASE + src
            r = sess.get(src, timeout=10)
            if r.status_code == 200:
                js_texts.append(r.text[:200000])
                print(f"  Fetched {src[:70]} ({len(r.text)} chars)")
        except Exception:
            pass

    # Step 3: Find and probe endpoints
    print("\n[3] Probing API endpoints …")
    endpoints = find_api_endpoints(html, js_texts)
    print(f"  {len(endpoints)} endpoints to probe")
    results = probe_endpoints(endpoints)

    endpoints_tried = [e for e in endpoints]

    # Step 4: Collect school rows from probed endpoints
    print("\n[4] Collecting school rows …")
    districts_raw = None
    for r in results:
        if r["rows"]:
            all_rows.extend(r["rows"])
        elif r.get("raw") and isinstance(r["raw"], list):
            # This might be a district list
            first = r["raw"][0] if r["raw"] else {}
            if isinstance(first, dict):
                keys_lower = {k.lower() for k in first}
                if any(k in keys_lower for k in ["district","district_id","dist_id"]):
                    print(f"  Found district list: {len(r['raw'])} districts from {r['url']}")
                    districts_raw = r["raw"]

    # Step 5: If we found a district list, drill into tehsils/schools
    if districts_raw and not all_rows:
        print("\n[5] Drilling district → tehsil → schools …")
        all_rows.extend(drill(None, "POST", districts_raw, sess))

    # Deduplicate
    seen, unique = set(), []
    for r in all_rows:
        k = (r.get("school_name",""), r.get("district",""))
        if k not in seen:
            seen.add(k)
            unique.append(r)

    print(f"\nTotal unique schools: {len(unique)}")
    save_csv(unique, ts)
    save_json(unique, ts, endpoints_tried)

    if not unique:
        print("\n⚠ Still 0 rows. AJAX captured:")
        for e in ajax[:10]:
            print(f"  {e['status']}  {e['url']}")
        print("\nEndpoints tried:")
        for ep in endpoints_tried[:15]:
            print(f"  {ep}")