"""
fetch.py — SIS PESRP Scraper v4  (FINAL)
=========================================
Discovered endpoints (from v3 log):
  GET  /user/get_districts          -> {"html": "<option value='1'>Attock</option>..."}
  POST /user/get_tehsils            -> {"html": "<option>...</option>"}
  POST /user/get_markazes           -> {"html": "<option>...</option>"}
  POST /user/get_schools            -> {"html": "<option>...</option>"}

Site uses CodeIgniter with CSRF token.
We get the token from the session cookie, then POST it with every request.

Data collected per school:
  - School name, EMIS/ID
  - District, Tehsil, Markaz
  - Total students, Boys, Girls
  - Grade-wise enrollment (KG–10, boys/girls)
  - Teachers allocated
  - E-Transfer status (homepage)
"""

import json, csv, re, time, requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

BASE = "https://sis.pesrp.edu.pk"

# ── Session with persistent cookies (needed for CSRF) ─────────────────────────
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept":     "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer":    f"{BASE}/str/analysis",
})

def get_csrf():
    """Load the main page to get CSRF cookie from CodeIgniter."""
    try:
        r = S.get(f"{BASE}/str/analysis", timeout=20)
        # CSRF token is in cookie
        csrf = S.cookies.get("csrf_test_name", "")
        # Also check meta tag
        if not csrf:
            m = re.search(r'csrf_test_name["\s:\']+([a-f0-9]+)', r.text)
            if m: csrf = m.group(1)
        # Also check hidden inputs
        if not csrf:
            soup = BeautifulSoup(r.text, "html.parser")
            inp = soup.find("input", {"name": "csrf_test_name"})
            if inp: csrf = inp.get("value","")
        print(f"CSRF token: {csrf[:20] if csrf else 'NOT FOUND'}")
        return csrf
    except Exception as e:
        print(f"get_csrf error: {e}")
        return ""

def post_data(url, payload, csrf):
    """POST with CSRF token included."""
    if csrf:
        payload["csrf_test_name"] = csrf
    try:
        r = S.post(url, data=payload, timeout=15)
        # Update CSRF token from response (CodeIgniter rotates it)
        new_csrf = S.cookies.get("csrf_test_name", csrf)
        return r, new_csrf
    except Exception as e:
        print(f"  POST error {url}: {e}")
        return None, csrf

def parse_options(html_str):
    """Parse <option value='id'>Name</option> from HTML string."""
    opts = []
    soup = BeautifulSoup(html_str or "", "html.parser")
    for opt in soup.find_all("option"):
        val  = opt.get("value","").strip()
        name = opt.get_text(strip=True)
        skip = {"","0","select","all","--","select district",
                "select tehsil","select markaz","select school"}
        if val and name.lower() not in skip:
            opts.append((val, name))
    return opts

def clean(t): return re.sub(r"\s+"," ",(t or "")).strip()
def num(v):
    try: return int(re.sub(r"[^\d]","",str(v or 0)) or 0)
    except: return 0

# ── E-Transfer status from homepage ──────────────────────────────────────────
def get_etransfer():
    print("\nFetching E-Transfer status ...")
    try:
        r = S.get(BASE, timeout=20)
        text = r.text
        open_  = "applications are being accepted" in text.lower() \
                 and "not being accepted" not in text.lower()
        status = "OPEN" if open_ else "CLOSED"
        # Extract dates
        m = re.search(
            r'from\s+(\d{1,2}-\w{3}-\d{2,4})\s+to\s+(\d{1,2}-\w{3}-\d{2,4})',
            text, re.IGNORECASE)
        start = m.group(1) if m else ""
        end   = m.group(2) if m else ""
        # Last updated
        u = re.search(r'Last Updated.*?(\d{1,2}\s+\w+\s+\d{4}.*?)(?:\n|<)', text, re.IGNORECASE)
        updated = u.group(1).strip() if u else ""
        print(f"  E-Transfer: {status}  |  Last round: {start} to {end}")
        return {"status": status, "accepting": open_,
                "last_round_start": start, "last_round_end": end,
                "site_updated": updated}
    except Exception as e:
        print(f"  E-Transfer error: {e}")
        return {"status":"UNKNOWN","accepting":False}

# ── Get enrollment data for a school ─────────────────────────────────────────
def get_school_enrollment(school_id, district_id, tehsil_id, markaz_id, csrf):
    """
    Try multiple endpoints to get per-school enrollment + grade breakdown.
    The /str/analysis page loads chart data via AJAX when filters are applied.
    """
    enrollment = {
        "total_students": 0, "boys": 0, "girls": 0, "teachers": 0,
        "grades": {}
    }

    # Common enrollment endpoints for CodeIgniter SIS systems
    endpoints = [
        f"{BASE}/str/get_school_stats",
        f"{BASE}/str/get_enrollment",
        f"{BASE}/str/get_school_data",
        f"{BASE}/str/analysis/get_data",
        f"{BASE}/str/get_stats",
        f"{BASE}/str/school_stats",
        f"{BASE}/str/get_grade_data",
    ]

    payload = {
        "school_id":   school_id,
        "district_id": district_id,
        "tehsil_id":   tehsil_id,
        "markaz_id":   markaz_id,
        "school":      school_id,
        "district":    district_id,
        "tehsil":      tehsil_id,
        "markaz":      markaz_id,
        "type":        "school",
        "tab":         "enrollment",
    }

    for ep in endpoints:
        r, csrf = post_data(ep, {**payload}, csrf)
        if not r or r.status_code != 200:
            continue
        body = r.text.strip()
        if not body or body[0] not in ("[","{"): 
            continue
        try:
            data = r.json()
            parsed = parse_enrollment_json(data)
            if parsed:
                enrollment.update(parsed)
                print(f"      Got enrollment from {ep.split('/')[-1]}")
                break
        except Exception:
            pass

    return enrollment, csrf

def parse_enrollment_json(data):
    """Extract enrollment numbers from various JSON shapes."""
    result = {}
    if isinstance(data, dict):
        # Unwrap common wrappers
        for key in ("data","result","school","stats","enrollment"):
            if key in data and isinstance(data[key], (dict,list)):
                inner = data[key]
                if isinstance(inner, dict):
                    data = inner
                    break

        # Direct keys
        key_map = {
            "total_students": ["total","total_students","enrollment","enrolled","total_enrol"],
            "boys":           ["boys","male","male_enrollment","boys_enrol"],
            "girls":          ["girls","female","female_enrollment","girls_enrol"],
            "teachers":       ["teachers","teacher_count","allocated_teachers","tch"],
        }
        for field, keys in key_map.items():
            for k in keys:
                if k in data:
                    result[field] = num(data[k])
                    break

        # Grade-wise data
        grades = {}
        grade_labels = ["KG","1","2","3","4","5","6","7","8","9","10"]
        for g in grade_labels:
            key_b = f"grade_{g}_boys" if g != "KG" else "grade_kg_boys"
            key_g = f"grade_{g}_girls" if g != "KG" else "grade_kg_girls"
            key_t = f"grade_{g}" if g != "KG" else "grade_kg"
            # Try various naming patterns
            for kb in [key_b, f"g{g}b", f"grade{g}boys", f"class_{g}_boys"]:
                if kb.lower() in {k.lower() for k in data}:
                    b_val = data.get(kb) or data.get(kb.lower()) or 0
                    g_val = data.get(key_g) or data.get(key_g.lower()) or 0
                    grades[f"grade_{g}"] = {
                        "boys": num(b_val), "girls": num(g_val),
                        "total": num(b_val) + num(g_val)
                    }
                    break
        if grades:
            result["grades"] = grades

    return result

# ── Main scraper ──────────────────────────────────────────────────────────────
def scrape():
    ts      = datetime.now(timezone.utc).isoformat()
    schools = []

    # Step 0: CSRF token
    csrf = get_csrf()

    # Step 1: E-Transfer status
    etransfer = get_etransfer()

    # Step 2: Districts
    print("\nFetching districts ...")
    r = S.get(f"{BASE}/user/get_districts", timeout=15)
    resp_data = r.json() if r.text.strip().startswith("{") else {}
    html_str  = resp_data.get("html", r.text)
    districts = parse_options(html_str)
    print(f"  {len(districts)} districts found")

    if not districts:
        print("  No districts found — check CSRF or endpoint")
        return schools, etransfer, ts

    for d_id, d_name in districts:
        print(f"\nDistrict: {d_name} (id={d_id})")

        # Step 3: Tehsils for this district
        r2, csrf = post_data(f"{BASE}/user/get_tehsils",
                             {"district_id": d_id}, csrf)
        tehsils = []
        if r2 and r2.status_code == 200:
            try:
                td = r2.json()
                tehsils = parse_options(td.get("html", r2.text))
            except Exception:
                tehsils = parse_options(r2.text)
        print(f"  {len(tehsils)} tehsils")
        if not tehsils:
            tehsils = [("", "")]

        for t_id, t_name in tehsils:

            # Step 4: Markazs for this tehsil
            r3, csrf = post_data(f"{BASE}/user/get_markazes",
                                 {"tehsil_id": t_id, "district_id": d_id}, csrf)
            markazs = []
            if r3 and r3.status_code == 200:
                try:
                    md = r3.json()
                    markazs = parse_options(md.get("html", r3.text))
                except Exception:
                    markazs = parse_options(r3.text)
            if not markazs:
                markazs = [("", "")]

            for m_id, m_name in markazs:

                # Step 5: Schools for this markaz
                r4, csrf = post_data(f"{BASE}/user/get_schools", {
                    "markaz_id":   m_id,
                    "tehsil_id":   t_id,
                    "district_id": d_id,
                }, csrf)
                school_opts = []
                if r4 and r4.status_code == 200:
                    try:
                        sd = r4.json()
                        school_opts = parse_options(sd.get("html", r4.text))
                    except Exception:
                        school_opts = parse_options(r4.text)

                print(f"  {t_name}/{m_name}: {len(school_opts)} schools")

                for s_id, s_name in school_opts:
                    # Step 6: Enrollment data for this school
                    enr, csrf = get_school_enrollment(
                        s_id, d_id, t_id, m_id, csrf)

                    row = {
                        "school_id":      s_id,
                        "school_name":    s_name,
                        "district":       d_name,
                        "tehsil":         t_name,
                        "markaz":         m_name,
                        "total_students": enr.get("total_students", 0),
                        "boys":           enr.get("boys", 0),
                        "girls":          enr.get("girls", 0),
                        "teachers":       enr.get("teachers", 0),
                        # Grade-wise
                        "grade_KG_boys":  enr.get("grades",{}).get("grade_KG",{}).get("boys",0),
                        "grade_KG_girls": enr.get("grades",{}).get("grade_KG",{}).get("girls",0),
                        "grade_1_boys":   enr.get("grades",{}).get("grade_1",{}).get("boys",0),
                        "grade_1_girls":  enr.get("grades",{}).get("grade_1",{}).get("girls",0),
                        "grade_2_boys":   enr.get("grades",{}).get("grade_2",{}).get("boys",0),
                        "grade_2_girls":  enr.get("grades",{}).get("grade_2",{}).get("girls",0),
                        "grade_3_boys":   enr.get("grades",{}).get("grade_3",{}).get("boys",0),
                        "grade_3_girls":  enr.get("grades",{}).get("grade_3",{}).get("girls",0),
                        "grade_4_boys":   enr.get("grades",{}).get("grade_4",{}).get("boys",0),
                        "grade_4_girls":  enr.get("grades",{}).get("grade_4",{}).get("girls",0),
                        "grade_5_boys":   enr.get("grades",{}).get("grade_5",{}).get("boys",0),
                        "grade_5_girls":  enr.get("grades",{}).get("grade_5",{}).get("girls",0),
                        "grade_6_boys":   enr.get("grades",{}).get("grade_6",{}).get("boys",0),
                        "grade_6_girls":  enr.get("grades",{}).get("grade_6",{}).get("girls",0),
                        "grade_7_boys":   enr.get("grades",{}).get("grade_7",{}).get("boys",0),
                        "grade_7_girls":  enr.get("grades",{}).get("grade_7",{}).get("girls",0),
                        "grade_8_boys":   enr.get("grades",{}).get("grade_8",{}).get("boys",0),
                        "grade_8_girls":  enr.get("grades",{}).get("grade_8",{}).get("girls",0),
                        "grade_9_boys":   enr.get("grades",{}).get("grade_9",{}).get("boys",0),
                        "grade_9_girls":  enr.get("grades",{}).get("grade_9",{}).get("girls",0),
                        "grade_10_boys":  enr.get("grades",{}).get("grade_10",{}).get("boys",0),
                        "grade_10_girls": enr.get("grades",{}).get("grade_10",{}).get("girls",0),
                        "etransfer_status": etransfer.get("status",""),
                        "scraped_at":     ts,
                    }
                    schools.append(row)
                    time.sleep(0.3)  # polite delay

    return schools, etransfer, ts

# ── Save CSV ──────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "school_id","school_name","district","tehsil","markaz",
    "total_students","boys","girls","teachers",
    "grade_KG_boys","grade_KG_girls",
    "grade_1_boys","grade_1_girls",
    "grade_2_boys","grade_2_girls",
    "grade_3_boys","grade_3_girls",
    "grade_4_boys","grade_4_girls",
    "grade_5_boys","grade_5_girls",
    "grade_6_boys","grade_6_girls",
    "grade_7_boys","grade_7_girls",
    "grade_8_boys","grade_8_girls",
    "grade_9_boys","grade_9_girls",
    "grade_10_boys","grade_10_girls",
    "etransfer_status","scraped_at",
]

def save(schools, etransfer, ts):
    # CSV
    with open("schools.csv","w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(schools)
    print(f"schools.csv  -- {len(schools)} rows")

    # JSON
    tot  = sum(s.get("total_students",0) for s in schools)
    boys = sum(s.get("boys",0)           for s in schools)
    gls  = sum(s.get("girls",0)          for s in schools)
    tch  = sum(s.get("teachers",0)       for s in schools)

    out = {
        "scraped_at": ts,
        "source":     BASE,
        "etransfer":  etransfer,
        "summary": {
            "total_schools":  len(schools),
            "total_students": tot,
            "total_boys":     boys,
            "total_girls":    gls,
            "total_teachers": tch,
        },
        "schools": schools,
    }
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"data.json    -- {len(schools)} schools | {tot:,} students")

# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*55)
    print("  SIS PESRP Scraper v4 — FINAL")
    print("="*55)
    schools, etransfer, ts = scrape()
    print(f"\nTotal schools collected: {len(schools)}")
    save(schools, etransfer, ts)

    if not schools:
        print("\nStill 0 — printing full response from /user/get_districts:")
        csrf = get_csrf()
        r = S.get(f"{BASE}/user/get_districts", timeout=15)
        print(f"  Status: {r.status_code}")
        print(f"  Response: {r.text[:500]}")
