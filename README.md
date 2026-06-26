# sdf
data from sis
SIS PESRP Live Dashboard — GitHub Pages
A free, serverless dashboard that auto-fetches public data from
sis.pesrp.edu.pk every 6 hours and serves it
as a static GitHub Pages site.

What It Shows
Data	Source
E-Transfer status (Open / Closed)	Homepage
Last round dates	Homepage
Official notices	Homepage
SIS own "Last Updated" timestamp	Homepage
District list	/str/analysis
Enrollment / teacher tables (if rendered)	/str/analysis
Tech Stack
Layer	Tool	Cost
Hosting	GitHub Pages	Free
Scheduler	GitHub Actions cron	Free (public repo = unlimited)
Scraper	Python + Playwright (headless Chromium)	Free
Storage	data.json committed to repo	Free
🚀 Setup (5 Steps)
1. Fork / Create repo
Create a public GitHub repo (public = unlimited Actions minutes).

2. Push these files
.github/
  workflows/
    fetch-data.yml
fetch.py
index.html
README.md
3. Grant Actions write permission
Repo → Settings → Actions → General → Workflow permissions
Select Read and write permissions → Save

This lets the bot commit data.json back to the repo.

4. Enable GitHub Pages
Repo → Settings → Pages → Source → Deploy from branch → main / (root)

Your site will be live at https://YOUR_USERNAME.github.io/REPO_NAME

5. Trigger the first run manually
Repo → Actions → Fetch SIS PESRP Data → Run workflow

Watch the workflow run, then check that data.json appears in your repo
and that the dashboard loads correctly.

Cron Schedule
The workflow runs at 0 */6 * * * — every 6 hours (4×/day).
Edit fetch-data.yml to change frequency (minimum */5 = every 5 minutes on Actions).

Notes
Data is scraped only from publicly accessible, robots-allowed pages.
All data is self-reported by schools and is provisional.
This is not an official PMIU/PESRP product.
Troubleshooting
Issue	Fix
data.json not created	Check Actions log for Python errors
Dashboard shows "Could not load data.json"	Run workflow manually first
Numbers showing "—"	Site's AJAX may have changed; check browser DevTools for new API endpoints
Playwright install fails	Ensure ubuntu-latest runner and playwright install chromium --with-deps
