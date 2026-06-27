"""
fetch.py — SIS PESRP Scraper v5
================================
v4 findings:
  - Districts: WORKING (40 found) via GET /user/get_districts
  - CSRF token: WORKING
  - E-Transfer: WORKING (CLOSED)
  - Tehsils: FAILING — wrong POST parameter name

Fix: try all possible parameter name combinations for tehsils/markazs/schools
"""

import json, csv, re, time, requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

BASE = "https://sis.pesrp.edu.pk"

S = requests.Session()
S.headers.update({
    "User-Agent":        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept":            "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With":  "XMLHttpRequest",
    "Referer":           f"{BASE}/str/analysis",
})

# ── CSRF ──────────────────────────────────────────────────────────────────────
def get_csrf():
    r = S.get(f"{BASE}/str/analysis", timeout=20)
    csrf = S.cookies.get("csrf_test_name","")
    if not csrf:
        m = re.search(r'csrf_test_name["\s:\']+([a-f0-9]+)', r.text)
        if m: csrf = m.group(1)
    print(f"CSRF: {csrf[:16]}...")
    return csrf

def do_get(path, params=None):
    try:
        r = S.get(f"{BASE}{path}", params=params, timeout=15)
        return r
    except Exception as e:
        print(f"  GET error {path}: {e}")
        return None

def do_post(path, data, csrf):
    try:
        if csrf: data["csrf_test_name"] = csrf
        r = S.post(f"{BASE}{path}", data=data, timeout=15)
        new_csrf = S.cookies.get("csrf_test_name", csrf)
        return r, new_csrf
    except Exception as e:
        print(f"  POST error {path}: {e}")
        return None, csrf

def parse_options(html_str):
    opts = []
    soup = BeautifulSoup(html_str or "", "html.parser")
    skip = {"","0","select","all","--","select district",
            "select tehsil","select markaz","select school",
            "all districts","all tehsils","all markazs","all schools"}
    for opt in soup.find_all("option"):
        val  = (opt.get("value") or "").strip()
        name = opt.get_text(strip=True)
        if val and name.lower() not in skip:
            opts.append((val, name))
    return opts

def parse_resp(r):
    """Extract HTML options from response — handles both JSON wrapper and raw HTML."""
    if not r or r.status_code != 200:
        return []
    body = r.text.strip()
    if not body:
        return []
    # Try JSON wrapper {"html": "..."}
    if body.startswith("{"):
        try:
            d = r.json()
            html = d.get("html") or d.get("data") or d.get("options") or ""
            return parse_options(html)
        except Exception:
            pass
    # Raw HTML
    return parse_options(body)

def clean(t): return re.sub(r"\s+"," ",(t or "")).strip()
def num(v):
    try: return int(re.sub(r"[^\d]","",str(v or 0)) or 0)
    except: return 0

# ── Smart cascade — tries every param name combination ────────────────────────
def get_tehsils(d_id, csrf):
    """Try every known parameter name for district ID."""
    for params in [
        {"district_id": d_id},
        {"district":    d_id},
        {"did":         d_id},
        {"dist_id":     d_id},
        {"d_id":        d_id},
        {"id":          d_id},
    ]:
        # Try POST
        r, csrf = do_post("/user/get_tehsils", {**params}, csrf)
        opts = parse_resp(r)
        if opts:
            print(f"    tehsils param: {list(params.keys())[0]} -> {len(opts)} tehsils")
            return opts, csrf
        # Try GET
        r2 = do_get("/user/get_tehsils", params)
        opts2 = parse_resp(r2)
        if opts2:
            print(f"    tehsils GET param: {list(params.keys())[0]} -> {len(opts2)} tehsils")
            return opts2, csrf
    # Debug: print raw response of last attempt
    r, csrf = do_post("/user/get_tehsils", {"district_id": d_id}, csrf)
    if r:
        print(f"    tehsils raw ({r.status_code}): {r.text[:200]}")
    return [], csrf

def get_markazs(d_id, t_id, csrf):
    for params in [
        {"tehsil_id": t_id, "district_id": d_id},
        {"tehsil":    t_id, "district":    d_id},
        {"tid":       t_id, "did":         d_id},
        {"tehsil_id": t_id},
        {"tehsil":    t_id},
        {"id":        t_id},
    ]:
        r, csrf = do_post("/user/get_markazes", {**params}, csrf)
        opts = parse_resp(r)
        if opts:
            return opts, csrf
        r2 = do_get("/user/get_markazes", params)
        opts2 = parse_resp(r2)
        if opts2:
            return opts2, csrf
    return [], csrf

def get_schools(d_id, t_id, m_id, csrf):
    for params in [
        {"markaz_id": m_id, "tehsil_id": t_id, "district_id": d_id},
        {"markaz":    m_id, "tehsil":    t_id, "district":    d_id},
        {"mid":       m_id, "tid":       t_id, "did":         d_id},
        {"markaz_id": m_id, "tehsil_id": t_id},
        {"markaz_id": m_id},
        {"markaz":    m_id},
        {"id":        m_id},
    ]:
        r, csrf = do_post("/user/get_schools", {**params}, csrf)
        opts = parse_resp(r)
        if opts:
            return opts, csrf
        r2 = do_get("/user/get_schools", params)
        opts2 = parse_resp(r2)
        if opts2:
            return opts2, csrf
    return [], csrf

# ── Enrollment data per school ────────────────────────────────────────────────
def get_enrollment(s_id, d_id, t_id, m_id, csrf):
    """Try every enrollment endpoint the site might have."""
    enr = {"total_students":0,"boys":0,"girls":0,"teachers":0,"grades":{}}

    endpoints = [
        "/str/get_school_stats",
        "/str/get_enrollment",
        "/str/get_school_data",
        "/str/get_stats",
        "/str/get_data",
        "/str/school_stats",
        "/str/get_grade_data",
        "/str/analysis/get_data",
        "/str/chart_data",
        "/user/get_school_stats",
        "/user/get_enrollment",
    ]

    base_payload = {
        "school_id":   s_id, "school":   s_id,
        "district_id": d_id, "district": d_id,
        "tehsil_id":   t_id, "tehsil":   t_id,
        "markaz_id":   m_id, "markaz":   m_id,
        "type": "school", "tab": "enrollment",
    }

    for ep in endpoints:
        r, csrf = do_post(ep, {**base_payload}, csrf)
        if not r or r.status_code != 200:
            continue
        body = r.text.strip()
        if not body or body[0] not in ("[","{"):
            continue
        try:
            data   = r.json()
            parsed = parse_enr_json(data)
            if parsed.get("total_students") or parsed.get("boys"):
                enr.update(parsed)
                break
        except Exception:
            pass

    return enr, csrf

def parse_enr_json(data):
    result = {}
    if isinstance(data, dict):
        for key in ("data","result","school","stats","enrollment","response"):
            if key in data and isinstance(data[key], dict):
                data = data[key]; break

        fields = {
            "total_students": ["total","total_students","enrollment","enrolled","total_enrol"],
            "boys":           ["boys","male","male_enrollment","boys_enrol"],
            "girls":          ["girls","female","female_enrollment","girls_enrol"],
            "teachers":       ["teachers","teacher_count","allocated_teachers","tch"],
        }
        dl = {k.lower(): v for k,v in data.items()}
        for field, keys in fields.items():
            for k in keys:
                if k in dl:
                    result[field] = num(dl[k]); break

        # Grade breakdown
        grades = {}
        for g in ["KG","1","2","3","4","5","6","7","8","9","10"]:
            gl = g.lower()
            for kb in [f"grade_{gl}_boys",f"g{gl}b",f"class_{gl}_boys",f"boys_{gl}"]:
                if kb in dl:
                    for kgi in [f"grade_{gl}_girls",f"g{gl}g",f"class_{gl}_girls",f"girls_{gl}"]:
                        gv = num(dl.get(kgi,0))
                    grades[f"grade_{g}"] = {"boys":num(dl[kb]),"girls":gv,
                                            "total":num(dl[kb])+gv}
                    break
        if grades:
            result["grades"] = grades

    return result

# ── E-Transfer ────────────────────────────────────────────────────────────────
def get_etransfer():
    try:
        r = S.get(BASE, timeout=20)
        txt  = r.text
        open_ = ("applications are being accepted" in txt.lower()
                 and "not being accepted" not in txt.lower())
        m = re.search(
            r'from\s+(\d{1,2}[-\s]\w{3}[-\s]\d{2,4})\s+to\s+(\d{1,2}[-\s]\w{3}[-\s]\d{2,4})',
            txt, re.IGNORECASE)
        return {
            "status":           "OPEN" if open_ else "CLOSED",
            "accepting":        open_,
            "last_round_start": m.group(1) if m else "",
            "last_round_end":   m.group(2) if m else "",
        }
    except Exception as e:
        return {"status":"UNKNOWN","accepting":False}

# ── Main ──────────────────────────────────────────────────────────────────────
def scrape():
    ts     = datetime.now(timezone.utc).isoformat()
    csrf   = get_csrf()
    etrans = get_etransfer()
    print(f"E-Transfer: {etrans['status']}")

    # Districts
    r = do_get("/user/get_districts")
    districts = parse_resp(r)
    print(f"Districts: {len(districts)}")

    schools = []

    for d_id, d_name in districts:
        print(f"\nDistrict: {d_name}")

        tehsils, csrf = get_tehsils(d_id, csrf)
        if not tehsils:
            tehsils = [("","All")]

        for t_id, t_name in tehsils:
            markazs, csrf = get_markazs(d_id, t_id, csrf)
            if not markazs:
                markazs = [("","All")]

            for m_id, m_name in markazs:
                school_opts, csrf = get_schools(d_id, t_id, m_id, csrf)
                print(f"  {t_name}/{m_name}: {len(school_opts)} schools")

                for s_id, s_name in school_opts:
                    enr, csrf = get_enrollment(s_id, d_id, t_id, m_id, csrf)
                    g = enr.get("grades", {})

                    schools.append({
                        "school_id":       s_id,
                        "school_name":     s_name,
                        "district":        d_name,
                        "tehsil":          t_name,
                        "markaz":          m_name,
                        "total_students":  enr.get("total_students", 0),
                        "boys":            enr.get("boys", 0),
                        "girls":           enr.get("girls", 0),
                        "teachers":        enr.get("teachers", 0),
                        "grade_KG_boys":   g.get("grade_KG",{}).get("boys",0),
                        "grade_KG_girls":  g.get("grade_KG",{}).get("girls",0),
                        "grade_1_boys":    g.get("grade_1",{}).get("boys",0),
                        "grade_1_girls":   g.get("grade_1",{}).get("girls",0),
                        "grade_2_boys":    g.get("grade_2",{}).get("boys",0),
                        "grade_2_girls":   g.get("grade_2",{}).get("girls",0),
                        "grade_3_boys":    g.get("grade_3",{}).get("boys",0),
                        "grade_3_girls":   g.get("grade_3",{}).get("girls",0),
                        "grade_4_boys":    g.get("grade_4",{}).get("boys",0),
                        "grade_4_girls":   g.get("grade_4",{}).get("girls",0),
                        "grade_5_boys":    g.get("grade_5",{}).get("boys",0),
                        "grade_5_girls":   g.get("grade_5",{}).get("girls",0),
                        "grade_6_boys":    g.get("grade_6",{}).get("boys",0),
                        "grade_6_girls":   g.get("grade_6",{}).get("girls",0),
                        "grade_7_boys":    g.get("grade_7",{}).get("boys",0),
                        "grade_7_girls":   g.get("grade_7",{}).get("girls",0),
                        "grade_8_boys":    g.get("grade_8",{}).get("boys",0),
                        "grade_8_girls":   g.get("grade_8",{}).get("girls",0),
                        "grade_9_boys":    g.get("grade_9",{}).get("boys",0),
                        "grade_9_girls":   g.get("grade_9",{}).get("girls",0),
                        "grade_10_boys":   g.get("grade_10",{}).get("boys",0),
                        "grade_10_girls":  g.get("grade_10",{}).get("girls",0),
                        "etransfer_status": etrans["status"],
                        "scraped_at":      ts,
                    })
                    time.sleep(0.2)

    return schools, etrans, ts

# ── Save ──────────────────────────────────────────────────────────────────────
FIELDS = [
    "school_id","school_name","district","tehsil","markaz",
    "total_students","boys","girls","teachers",
    "grade_KG_boys","grade_KG_girls",
    "grade_1_boys","grade_1_girls","grade_2_boys","grade_2_girls",
    "grade_3_boys","grade_3_girls","grade_4_boys","grade_4_girls",
    "grade_5_boys","grade_5_girls","grade_6_boys","grade_6_girls",
    "grade_7_boys","grade_7_girls","grade_8_boys","grade_8_girls",
    "grade_9_boys","grade_9_girls","grade_10_boys","grade_10_girls",
    "etransfer_status","scraped_at",
]

def save(schools, etrans, ts):
    with open("schools.csv","w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(schools)

    tot  = sum(s.get("total_students",0) for s in schools)
    out  = {
        "scraped_at": ts, "source": BASE,
        "etransfer":  etrans,
        "summary": {
            "total_schools":  len(schools),
            "total_students": tot,
            "total_boys":     sum(s.get("boys",0)     for s in schools),
            "total_girls":    sum(s.get("girls",0)    for s in schools),
            "total_teachers": sum(s.get("teachers",0) for s in schools),
        },
        "schools": schools,
    }
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\nschools.csv -> {len(schools)} rows")
    print(f"data.json   -> {len(schools)} schools | {tot:,} students")

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*50)
    print("  SIS PESRP Scraper v5")
    print("="*50)
    schools, etrans, ts = scrape()
    print(f"\nTotal: {len(schools)} schools")
    save(schools, etrans, ts)
