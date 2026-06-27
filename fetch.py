"""
fetch.py — SIS PESRP Scraper
Intercepts AJAX calls on sis.pesrp.edu.pk/str/analysis,
drives District → Tehsil → Markaz → School dropdowns,
collects every school's enrollment data and saves:
  • schools.csv  — open in Excel / Google Sheets
  • data.json    — read by GitHub Pages dashboard
"""

import json, csv, re, time
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE   = "https://sis.pesrp.edu.pk"
URL    = f"{BASE}/str/analysis"

# ── AJAX interceptor ──────────────────────────────────────────────────────────
ajax = []   # all captured JSON responses

def on_response(resp):
    try:
        ct  = resp.headers.get("content-type", "")
        url = resp.url
        if "json" in ct or any(k in url for k in
                ["/str/","/api/","/get_","/fetch_","/load_","/stats/"]):
            body = resp.text()
            if body and body.strip()[:1] in ("[","{"): 
                ajax.append({"url": url, "status": resp.status, "body": body[:20000]})
    except Exception:
        pass

# ── Dropdown helpers ──────────────────────────────────────────────────────────
def options(page, sel):
    out = []
    try:
        for el in page.query_selector_all(f"{sel} option"):
            v = el.get_attribute("value") or ""
            t = (el.inner_text() or "").strip()
            skip = {"","select","all","--","select district","select tehsil",
                    "select markaz","select school","select all"}
            if v and t.lower() not in skip:
                out.append((v, t))
    except Exception:
        pass
    return out

def find(page, *keywords):
    for kw in keywords:
        for el in page.query_selector_all("select"):
            nm = (el.get_attribute("name") or el.get_attribute("id") or "").lower()
            if kw in nm:
                a = "name" if el.get_attribute("name") else "id"
                return f"select[{a}='{el.get_attribute(a)}']"
    return None

def pick(page, sel, val, wait=2.0):
    try:
        page.select_option(sel, value=val)
        try: page.wait_for_load_state("networkidle", timeout=5000)
        except Exception: pass
        time.sleep(wait)
        return True
    except Exception:
        return False

def clean(t): return re.sub(r"\s+", " ", (t or "")).strip()
def num(v):
    try: return int(re.sub(r"[^\d]", "", str(v or 0)) or 0)
    except: return 0

# ── Parse JSON from AJAX ──────────────────────────────────────────────────────
NAME_K  = ["school_name","name","school","sch_name","school_title"]
TOT_K   = ["total","total_students","enrollment","students","enrolled"]
BOYS_K  = ["boys","male","male_enrollment","boy_count"]
GIRLS_K = ["girls","female","female_enrollment","girl_count"]
TCH_K   = ["teachers","teacher_count","allocated_teachers","staff"]
DIST_K  = ["district","district_name","dist_name"]
TEH_K   = ["tehsil","tehsil_name","teh_name"]
MRK_K   = ["markaz","markaz_name","circle"]
ID_K    = ["school_id","id","emis","emis_code","school_code"]

def gf(row, keys):
    rl = {k.lower(): v for k, v in row.items()}
    for k in keys:
        if k in rl: return rl[k]
    return ""

def parse_ajax(data, dist="", teh="", mrk="", ts=""):
    rows = []
    if isinstance(data, dict):
        for key in ("data","result","schools","rows","records","items"):
            if key in data and isinstance(data[key], list):
                data = data[key]; break
        else:
            return rows
    if not isinstance(data, list): return rows

    for item in data:
        if not isinstance(item, dict): continue
        keys = {k.lower() for k in item}
        if not any(k in keys for k in
                   ["school","emis","enrollment","students","boys","girls"]):
            continue
        b = num(gf(item, BOYS_K))
        g = num(gf(item, GIRLS_K))
        t = num(gf(item, TOT_K)) or b + g
        rows.append({
            "school_id":      str(gf(item, ID_K) or ""),
            "school_name":    clean(gf(item, NAME_K)) or "Unknown",
            "district":       clean(gf(item, DIST_K)) or dist,
            "tehsil":         clean(gf(item, TEH_K))  or teh,
            "markaz":         clean(gf(item, MRK_K))  or mrk,
            "total_students": t,
            "boys":           b,
            "girls":          g,
            "teachers":       num(gf(item, TCH_K)),
            "scraped_at":     ts,
        })
    return rows

# ── Table scraping fallback ───────────────────────────────────────────────────
def scrape_table(page, dist, teh, mrk, ts):
    rows = []
    for tbl in page.query_selector_all("table"):
        hdrs = [clean(h.inner_text()).lower()
                for h in tbl.query_selector_all("th")]
        for tr in tbl.query_selector_all("tbody tr"):
            cells = [clean(td.inner_text())
                     for td in tr.query_selector_all("td")]
            if not cells: continue
            row = dict(zip(hdrs, cells)) if hdrs else {}
            if not row and cells:
                row = {"school_name": cells[0]}
                for i, c in enumerate(cells[1:], 1):
                    row[f"col{i}"] = c
            parsed = parse_ajax([row], dist, teh, mrk, ts)
            rows.extend(parsed)
    return rows

# ── Main ──────────────────────────────────────────────────────────────────────
def scrape():
    ts   = datetime.now(timezone.utc).isoformat()
    rows = []
    prev = 0

    def flush_ajax(dist="", teh="", mrk=""):
        nonlocal prev
        found = []
        for entry in ajax[prev:]:
            try: data = json.loads(entry["body"])
            except: continue
            r = parse_ajax(data, dist, teh, mrk, ts)
            if r:
                print(f"    ✓ AJAX  {entry['url'][:70]}  → {len(r)} rows")
                found.extend(r)
        prev = len(ajax)
        return found

    with sync_playwright() as pw:
        br  = pw.chromium.launch(headless=True)
        ctx = br.new_context(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124 Safari/537.36"))
        pg  = ctx.new_page()
        pg.on("response", on_response)

        print(f"Opening {URL} …")
        try:
            pg.goto(URL, wait_until="networkidle", timeout=60000)
        except PWTimeout:
            print("❌ Timeout"); br.close(); return rows, ts

        time.sleep(4)
        flush_ajax()   # capture whatever loaded on page-open

        # Locate dropdowns
        d_sel = find(pg, "district")
        t_sel = find(pg, "tehsil")
        m_sel = find(pg, "markaz", "circle")
        s_sel = find(pg, "school")

        print(f"Selectors → district:{d_sel}  tehsil:{t_sel}  "
              f"markaz:{m_sel}  school:{s_sel}")

        districts = options(pg, d_sel) if d_sel else []
        print(f"Districts found: {len(districts)}")

        if not districts:
            # No dropdowns — grab whatever is visible
            rows = scrape_table(pg, "", "", "", ts)
            br.close()
            return rows, ts

        for dv, dn in districts:
            print(f"\n📍 {dn}")
            if d_sel: pick(pg, d_sel, dv)
            r = flush_ajax(dn)
            rows.extend(r)

            tehsils = options(pg, t_sel) if t_sel else [("","")]
            for tv, tn in tehsils:
                if t_sel and tv: pick(pg, t_sel, tv, 1.5)
                r = flush_ajax(dn, tn)
                rows.extend(r)

                markazs = options(pg, m_sel) if m_sel else [("","")]
                for mv, mn in markazs:
                    if m_sel and mv: pick(pg, m_sel, mv, 1.5)
                    r = flush_ajax(dn, tn, mn)
                    rows.extend(r)

                    if s_sel:
                        schools = options(pg, s_sel)
                        print(f"  {tn}/{mn}: {len(schools)} schools")
                        for sv, sn in schools:
                            pick(pg, s_sel, sv, 1.0)
                            r = flush_ajax(dn, tn, mn)
                            if not r:
                                r = scrape_table(pg, dn, tn, mn, ts)
                            if not r:
                                r = [{"school_id":"","school_name":sn,
                                      "district":dn,"tehsil":tn,"markaz":mn,
                                      "total_students":0,"boys":0,"girls":0,
                                      "teachers":0,"scraped_at":ts}]
                            rows.extend(r)
                    else:
                        r = scrape_table(pg, dn, tn, mn, ts)
                        if r:
                            print(f"  {tn}/{mn}: {len(r)} rows (table)")
                            rows.extend(r)

        br.close()

    # Deduplicate by (school_name, district)
    seen, unique = set(), []
    for r in rows:
        key = (r["school_name"], r["district"])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return unique, ts

# ── Save CSV ──────────────────────────────────────────────────────────────────
FIELDS = ["school_id","school_name","district","tehsil","markaz",
          "total_students","boys","girls","teachers","scraped_at"]

def save_csv(rows):
    with open("schools.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"✓ schools.csv  ({len(rows)} rows)")

# ── Save JSON ─────────────────────────────────────────────────────────────────
def save_json(rows, ts):
    tot = sum(r["total_students"] for r in rows)
    boy = sum(r["boys"]           for r in rows)
    grl = sum(r["girls"]          for r in rows)
    tch = sum(r["teachers"]       for r in rows)
    out = {
        "scraped_at": ts,
        "source":     BASE,
        "summary": {
            "total_schools":  len(rows),
            "total_students": tot,
            "total_boys":     boy,
            "total_girls":    grl,
            "total_teachers": tch,
        },
        "schools": rows,
        "ajax_endpoints": [
            {"url": e["url"], "status": e["status"]}
            for e in ajax[:30]
        ],
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"✓ data.json    ({len(rows)} schools | {tot:,} students)")

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  SIS PESRP Scraper")
    print("=" * 50)
    rows, ts = scrape()
    print(f"\nTotal rows: {len(rows)}")
    save_csv(rows)
    save_json(rows, ts)

    if not rows:
        print("\n⚠ No data collected.")
        print("   AJAX endpoints captured:")
        for e in ajax[:10]:
            print(f"   {e['status']}  {e['url']}")