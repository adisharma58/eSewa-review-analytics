# eSewa Review Pipeline

Scrapes eSewa's iOS App Store reviews once a day, stores them as CSVs in this
repo, and serves a live analytics dashboard (hosted on Vercel) that always
shows the latest data — no manual upload, no webhook, no "ping" needed.

## How it fits together

```
GitHub Actions (cron, daily)
   -> runs scraper/esewa_ios_reviews.py
   -> writes data/daily/<date>.csv, appends data/monthly/<month>.csv
   -> updates data/manifest.json (index of what files exist)
   -> commits + pushes the new files back to this repo

Vercel (static hosting of site/index.html)
   -> the page's own JS fetches data/manifest.json + the monthly CSVs
      directly from raw.githubusercontent.com, client-side, on every load
   -> no redeploy, no server, no webhook required — the browser always
      asks GitHub for the freshest committed file
```

Because the dashboard fetches its data at *page-load time* straight from
GitHub, you don't need to redeploy Vercel or "ping" it when new data lands.
The Action commits → the next time anyone opens the dashboard (or clicks
"Sync latest"), they get the new rows. This is simpler and has no moving
parts to break.

## Repo layout

```
.github/workflows/scrape.yml   GitHub Action: runs daily, commits new CSVs
scraper/esewa_ios_reviews.py   The scraper (incremental, dedupe-aware)
scraper/requirements.txt
data/
  daily/YYYY-MM-DD.csv         only the reviews that were NEW on that day
  monthly/YYYY-MM.csv          running append-only log for the whole month
  seen_ids.json                dedupe ledger so the same review is never
                                counted twice across runs
  manifest.json                index the dashboard reads first, to know
                                which monthly files exist
site/index.html                the dashboard (deploy this to Vercel)
```

### Why "daily" and "monthly" files, not weekly?
"Weekly" and "monthly" views in the dashboard itself are just date-range
filters (it already has Today / 7D / 30D / All / Custom presets) over
whatever monthly CSVs you've loaded — you don't need separate weekly files
for that. Daily and monthly files exist to keep individual commits small
and let the dashboard load only the recent months it needs.

### How reviews get filed
Apple's review feed has no concept of a date range — each run just returns
the most recent reviews per storefront. Every review already carries its
own posted date though, so instead of dumping everything a run finds into
"today's" file, the scraper:

1. Figures out "today" in **Nepal time** first (the daily cron is
   deliberately scheduled for 00:15 NPT, so by run time all of "yesterday"
   NPT is finished and safe to treat as complete).
2. Converts each review's own posted timestamp to an NPT calendar date and
   files it under `data/daily/<that date>.csv` and `data/monthly/<that
   month>.csv` — not the date the script happened to run.
3. Skips any review posted **today** entirely (not written anywhere, not
   marked as seen) — today's count is still incomplete and will keep
   climbing until the day is over, so it's picked up on a later run once
   it's no longer "today".
4. After writing, checks whether every day from the 1st of the current
   month through yesterday has a `data/daily/<date>.csv` file yet, and
   prints a warning listing any that don't. This mostly self-heals (a
   missed day's reviews are often still in Apple's recent-reviews window
   and get caught on a later run), but on a very high-volume day a review
   could in theory age out of that window before any run ever sees it —
   the warning is a visibility check, not a guarantee of zero gaps.

## Setup

### 1. Create the repo
1. Create a new **public** GitHub repo, e.g. `esewa-review-pipeline`.
2. Push these files to it (`git init`, `git add .`, `git commit`, `git remote add origin ...`, `git push`).
   The `data/` folder can start empty except for an empty `.gitkeep` — the
   first Action run will populate it.

### 2. Enable Actions permissions
By default, GitHub Actions' token is read-only. To let the workflow commit
CSVs back:
1. Repo → **Settings → Actions → General → Workflow permissions**
2. Select **"Read and write permissions"**, save.

### 3. Run it once manually
Repo → **Actions** tab → "Daily eSewa review scrape" → **Run workflow**.
This does the first scrape and creates `data/manifest.json`, so the
dashboard has something to fetch. After this, it also runs automatically
every day on the cron schedule in `scrape.yml` (edit the `cron:` line if
you want a different time — [crontab.guru](https://crontab.guru) helps).

### 4. Point the dashboard at your repo
In `site/index.html`, find:

```js
const GITHUB_CONFIG = {
  owner: "YOUR_GITHUB_USERNAME",     // <-- change this
  repo: "esewa-review-pipeline",     // <-- change this (repo name)
  branch: "main",
};
```

Set `owner` and `repo` to match your GitHub username and repo name, commit,
and push.

### 5. Deploy the dashboard to Vercel
1. [vercel.com](https://vercel.com) → **Add New Project** → import this repo.
2. Framework preset: **Other** (it's a static HTML file, no build step).
3. **Root Directory**: set to `site` (so Vercel serves `site/index.html` as
   the site root).
4. Deploy. That's it — no environment variables, no serverless functions.

Every time someone visits the Vercel URL, the page tries to silently pull
the last 3 months of data straight from `raw.githubusercontent.com`. If
that succeeds, the dashboard opens straight into the charts. If it can't
reach GitHub (e.g. you haven't set `GITHUB_CONFIG` yet, or the Action
hasn't run), it just falls back to the normal drag-and-drop upload screen
— nothing breaks either way.

There are also two manual buttons if you don't want to rely on auto-load:
- **Sync latest** — pulls the last 3 months' CSVs from GitHub.
- **Load full history** — pulls every monthly CSV listed in the manifest.

## Extending to Android
The dashboard already understands an `Android` platform column (it
auto-detects Play Store vs App Store exports by filename/headers), but the
scraper here only covers iOS. If you want Android too, the
[`google-play-scraper`](https://pypi.org/project/google-play-scraper/)
Python package can pull Play Store reviews in the same shape; write it into
`data/daily/` and `data/monthly/` the same way (same `FIELDNAMES`, add a
`platform` marker in the filename so `detectPlatform()` picks it up) and
the dashboard will merge both automatically.

## Local testing
```bash
cd scraper
pip install -r requirements.txt
python esewa_ios_reviews.py
```
Then open `site/index.html` directly in a browser and drag in the CSV it
just produced from `data/daily/` or `data/monthly/`, to sanity-check
formatting before you push to GitHub.
