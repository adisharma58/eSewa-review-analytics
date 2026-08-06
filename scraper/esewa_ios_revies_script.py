"""
eSewa iOS App Store Review Scraper — incremental / repo-friendly version
-------------------------------------------------------------------------
Pulls customer ratings & reviews for the eSewa app from Apple's public
RSS review feed, across every App Store country storefront, and writes
them into a repo-friendly, date-partitioned layout:

    data/daily/<YYYY-MM-DD>.csv     -> reviews whose OWN posted date is that day
    data/monthly/<YYYY-MM>.csv      -> running append-only log for that month
    data/seen_ids.json              -> dedupe ledger (persisted across runs)
    data/manifest.json              -> index of what files exist + last run info

This is designed to run once a day inside GitHub Actions, where the
runner is thrown away after every job. The "state" that lets us know
which reviews are new is `data/seen_ids.json`, which is committed back
to the repo at the end of each run (see .github/workflows/scrape.yml).

Routing logic (this is the important part)
--------------------------------------------
Every review already carries its own posted date (Apple's `updated`
field), so instead of dumping everything this run found into
"today's" file, each review is filed under the day/month it actually
belongs to:

  1. Figure out "today" first, in Nepal time (Asia/Kathmandu) -- eSewa
     is a Nepal-focused app, and the daily cron is deliberately
     scheduled for 00:15 NPT so that, by the time it runs, all of
     "yesterday" (NPT) is finished and safe to treat as complete.
  2. Convert each review's own posted timestamp to an NPT calendar date.
  3. Reviews posted TODAY (NPT) are skipped entirely -- not written to
     any file, and not added to the seen-ids ledger -- because "today"
     is still an incomplete, growing bucket. They'll be picked up
     naturally on a future run once their date is no longer "today".
  4. Everything else gets appended to data/daily/<its own date>.csv and
     data/monthly/<its own month>.csv, creating those files the first
     time something lands in them.
  5. After writing, the script checks whether every day from day 1 of
     the current month through yesterday already has a daily file. Any
     that don't are reported as "missing days" -- there's no way to
     force-fetch a specific historical date from Apple's feed (it only
     ever returns the most recent ~500 reviews per storefront), so this
     is a visibility check, not something the script can always fix:
     most of the time it self-heals (a review from a missed day is
     still sitting in the feed's recent window and gets caught on a
     later run), but on a very high-review-volume day it's possible for
     a review to age out of that window before any run ever sees it.

No API key needed -- uses Apple's free public RSS feed:
https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortby=mostrecent/page={page}/json

Requirements:
    pip install -r requirements.txt

Usage:
    python esewa_ios_reviews.py
"""

import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# eSewa is Nepal-focused -- "today" and each review's day/month bucket are
# computed in Nepal time, not UTC, to match how the team actually thinks
# about "today" (and to match the cron schedule in scrape.yml, which is
# deliberately set to 00:15 NPT).
NPT = ZoneInfo("Asia/Kathmandu")

# eSewa's App Store app ID (from apps.apple.com/.../app/esewa/id614370939)
APP_ID = "614370939"

# Every 2-letter country code Apple runs a storefront in.
COUNTRIES = [
    "dz","ao","ai","ag","ar","am","au","at","az","bh","bd","bb","by","be","bz","bj",
    "bm","bt","bo","bw","br","vg","bn","bg","bf","kh","ca","cv","ky","td","cl","cn",
    "co","cg","cr","hr","cy","cz","dk","dm","do","ec","eg","sv","ee","fj","fi","fr",
    "ga","gm","de","gh","gr","gd","gt","gw","gy","hn","hk","hu","is","in","id","ie",
    "il","it","jm","jp","jo","kz","ke","kr","kw","kg","la","lv","lb","lr","lt","lu",
    "mo","mk","mg","mw","my","mv","ml","mt","mr","mu","mx","fm","md","mn","ms","ma",
    "mz","mm","na","np","nl","nz","ni","ne","ng","no","om","pk","pw","pa","pg","py",
    "pe","ph","pl","pt","qa","ro","ru","rw","sa","sn","sc","sl","sg","sk","si","sb",
    "za","es","lk","kn","lc","vc","sr","sz","se","ch","tw","tj","tz","th","tn","tr",
    "tm","tc","ug","ua","ae","gb","us","uy","uz","ve","vn","ye","zm","zw","bs","bf"
]

MAX_PAGES = 10        # Apple's RSS feed caps out around page 10 per storefront
REQUEST_DELAY = 0.5   # seconds between requests, to stay polite to Apple's servers

FIELDNAMES = [
    "country", "review_id", "author", "rating", "title",
    "review_text", "date", "app_version"
]

# All paths are relative to the repo root, since the GitHub Actions
# workflow runs this script with the repo root as the working directory.
DATA_DIR = "data"
DAILY_DIR = os.path.join(DATA_DIR, "daily")
MONTHLY_DIR = os.path.join(DATA_DIR, "monthly")
SEEN_IDS_PATH = os.path.join(DATA_DIR, "seen_ids.json")
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")


def fetch_reviews_for_country(country: str):
    """Fetch all review pages for one country storefront."""
    reviews = []
    for page in range(1, MAX_PAGES + 1):
        url = (
            f"https://itunes.apple.com/{country}/rss/customerreviews/"
            f"id={APP_ID}/sortby=mostrecent/page={page}/json"
        )
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                break
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            break

        entries = data.get("feed", {}).get("entry")
        if not entries:
            break  # no more reviews for this storefront

        # Apple returns a single dict (not a list) if there's only one entry
        if isinstance(entries, dict):
            entries = [entries]

        # The first "entry" on page 1 is sometimes just app metadata, not a review.
        # Real reviews always have an "author" and "rating" field.
        for e in entries:
            if "author" not in e or "im:rating" not in e:
                continue
            reviews.append({
                "country": country,
                "review_id": e.get("id", {}).get("label", ""),
                "author": e.get("author", {}).get("name", {}).get("label", ""),
                "rating": e.get("im:rating", {}).get("label", ""),
                "title": e.get("title", {}).get("label", ""),
                "review_text": e.get("content", {}).get("label", "").replace("\n", " ").strip(),
                "date": e.get("updated", {}).get("label", ""),
                "app_version": e.get("im:version", {}).get("label", ""),
            })

        time.sleep(REQUEST_DELAY)

    return reviews


def parse_review_date_npt(date_label: str):
    """Convert Apple's ISO-8601 'updated' timestamp to an NPT calendar date string
    (YYYY-MM-DD). Returns None if the timestamp can't be parsed."""
    if not date_label:
        return None
    try:
        dt = datetime.fromisoformat(date_label)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(NPT).strftime("%Y-%m-%d")


def missing_days_this_month(now_npt: datetime):
    """Days from day 1 of the current NPT month through yesterday that don't
    have a data/daily/<date>.csv file yet."""
    first_of_month = now_npt.replace(day=1)
    yesterday = now_npt - timedelta(days=1)
    if yesterday.month != now_npt.month or yesterday.year != now_npt.year:
        return []  # today is the 1st -- nothing in this month to check yet
    missing = []
    d = first_of_month
    while d.date() <= yesterday.date():
        date_str = d.strftime("%Y-%m-%d")
        if not os.path.exists(os.path.join(DAILY_DIR, f"{date_str}.csv")):
            missing.append(date_str)
        d += timedelta(days=1)
    return missing


def load_seen_ids() -> set:
    if not os.path.exists(SEEN_IDS_PATH):
        return set()
    with open(SEEN_IDS_PATH, "r", encoding="utf-8") as f:
        try:
            return set(json.load(f))
        except json.JSONDecodeError:
            return set()


def save_seen_ids(seen_ids: set):
    with open(SEEN_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_ids), f, indent=0)


def append_csv(path: str, rows: list):
    """Append rows to a CSV, writing a header only if the file is new."""
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)



def update_manifest(committed_count: int, total_seen: int, skipped_today: int, missing_days: list):
    daily_files = sorted(f"daily/{f}" for f in os.listdir(DAILY_DIR) if f.endswith(".csv"))
    monthly_files = sorted(f"monthly/{f}" for f in os.listdir(MONTHLY_DIR) if f.endswith(".csv"))

    manifest = {
        "last_run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_run_new_reviews": committed_count,
        "last_run_skipped_today": skipped_today,
        "total_reviews_seen": total_seen,
        "daily_files": daily_files,
        "monthly_files": monthly_files,
        "missing_days_current_month": missing_days,
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def main():
    os.makedirs(DAILY_DIR, exist_ok=True)
    os.makedirs(MONTHLY_DIR, exist_ok=True)

    seen_ids = load_seen_ids()
    starting_seen_count = len(seen_ids)

    # Pass 1: fetch everything, dedupe against the persisted ledger AND against
    # duplicates of the same review turning up under more than one storefront
    # in this same run. Nothing is added to the persisted ledger yet -- that
    # only happens for reviews we actually decide to commit below.
    candidates = []
    candidate_keys_this_run = set()

    for i, country in enumerate(COUNTRIES, 1):
        print(f"[{i}/{len(COUNTRIES)}] Fetching reviews for '{country}'...")
        country_reviews = fetch_reviews_for_country(country)

        new_count = 0
        for r in country_reviews:
            key = r["review_id"] or f'{r["author"]}|{r["title"]}|{r["date"]}'
            if key in seen_ids or key in candidate_keys_this_run:
                continue
            candidate_keys_this_run.add(key)
            r["_key"] = key
            candidates.append(r)
            new_count += 1

        print(f"    -> {len(country_reviews)} found, {new_count} new")

    # Pass 2: figure out "today" in Nepal time, then route each candidate to
    # the day/month it actually belongs to -- skipping anything posted today.
    now_npt = datetime.now(timezone.utc).astimezone(NPT)
    today_str = now_npt.strftime("%Y-%m-%d")

    by_day, by_month, committed_keys = {}, {}, []
    skipped_today = 0
    skipped_unparseable = 0

    for r in candidates:
        review_date = parse_review_date_npt(r["date"])
        if review_date is None:
            skipped_unparseable += 1
            continue  # don't mark as seen -- we'll try parsing it again next run
        if review_date == today_str:
            skipped_today += 1
            continue  # today is still incomplete -- don't persist or mark seen yet

        row = {k: v for k, v in r.items() if k != "_key"}
        by_day.setdefault(review_date, []).append(row)
        by_month.setdefault(review_date[:7], []).append(row)
        committed_keys.append(r["_key"])

    for date_str, rows in sorted(by_day.items()):
        append_csv(os.path.join(DAILY_DIR, f"{date_str}.csv"), rows)
    for month_str, rows in sorted(by_month.items()):
        append_csv(os.path.join(MONTHLY_DIR, f"{month_str}.csv"), rows)

    seen_ids.update(committed_keys)
    save_seen_ids(seen_ids)

    missing = missing_days_this_month(now_npt)
    update_manifest(len(committed_keys), len(seen_ids), skipped_today, missing)

    print(f"\nDone. {len(committed_keys)} reviews committed this run "
          f"(seen ledger grew from {starting_seen_count} to {len(seen_ids)}).")
    print(f"Skipped (today, NPT {today_str}, still incomplete): {skipped_today}")
    if skipped_unparseable:
        print(f"Skipped (unparseable date): {skipped_unparseable}")
    if by_day:
        print("Day files updated:  ", ", ".join(sorted(by_day.keys())))
    if by_month:
        print("Month files updated:", ", ".join(sorted(by_month.keys())))

    if missing:
        print(f"\nWARNING: no data yet for {len(missing)} day(s) this month: {', '.join(missing)}")
        print("This can still self-heal on a later run if those reviews are sitting in")
        print("Apple's recent-reviews window -- or it may genuinely mean zero reviews that day.")
    else:
        print("\nNo gaps: every day this month (through yesterday) has at least one file.")


if __name__ == "__main__":
    main()
