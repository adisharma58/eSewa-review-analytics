"""
eSewa Android (Google Play) Review Scraper — incremental / repo-friendly version
----------------------------------------------------------------------------------
Mirrors scraper/esewa_ios_reviews.py's design and routing logic (see that
file for the full rationale) but pulls from the Google Play Store instead
of Apple's App Store.

Google doesn't expose a public, no-auth review feed the way Apple does, so
this uses the unofficial `google-play-scraper` library, which reads the
same data the Play Store website itself displays -- no API key or Play
Console access required.

Writes into a SEPARATE data/android/ tree so Android's column schema never
gets mixed into the same CSVs as iOS (different fields, different review
ID format, etc.):

    data/android/daily/<YYYY-MM-DD>.csv
    data/android/monthly/<YYYY-MM>.csv
    data/android/seen_ids.json
    data/android/manifest.json

Routing logic (identical intent to the iOS script)
----------------------------------------------------
Every review already carries its own posted date, so instead of dumping
everything a run finds into "today's" file, each review is filed under
the day/month it actually belongs to:

  1. Figure out "today" first, in Nepal time (Asia/Kathmandu) -- matches
     the iOS script and the cron schedule in scrape.yml (00:15 NPT, so
     "yesterday" NPT is always finished and safe to treat as complete).
  2. Convert each review's own posted timestamp to an NPT calendar date.
  3. Reviews posted TODAY (NPT) are skipped entirely -- not written to
     any file, not marked as seen -- because "today" is still growing.
     They're picked up naturally on a later run once they're no longer
     "today".
  4. Everything else is appended to data/android/daily/<its own date>.csv
     and data/android/monthly/<its own month>.csv.
  5. After writing, checks whether every day from day 1 of the current
     month through yesterday has a daily file, and reports any that
     don't. Unlike Apple's feed, Google Play doesn't hard-cap you at a
     fixed number of recent reviews -- but this is still a visibility
     check rather than a guarantee, since a review could in theory be
     missed if it's old enough to be paginated past before a run ever
     reaches it (see EARLY_STOP_AFTER_SEEN below).

Pagination / incremental-fetch strategy
------------------------------------------
Unlike Apple's feed (hard-capped at ~10 pages), Google Play lets you page
through a huge amount of review history via a continuation token. Paging
all the way through that every single day would be slow and pointless
once you're caught up, so each locale's fetch sorts NEWEST-first and
stops early once it hits a run of reviews that are already in the seen
ledger (EARLY_STOP_AFTER_SEEN consecutive) -- i.e. "we've clearly caught
up with what we already have". On the very first run (no ledger yet) it
will page much further, up to MAX_PAGES_PER_LOCALE, to backfill history.

Requirements:
    pip install -r requirements.txt   (adds google-play-scraper)

Usage:
    python esewa_android_reviews.py
"""

import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google_play_scraper import Sort, reviews

# eSewa is Nepal-focused -- "today" and each review's day/month bucket are
# computed in Nepal time, not UTC, matching the iOS script and the cron
# schedule in scrape.yml.
NPT = ZoneInfo("Asia/Kathmandu")

# eSewa - Mobile Wallet (Nepal) on Google Play
# (play.google.com/store/apps/details?id=com.f1soft.esewa)
APP_ID = "com.f1soft.esewa"

# Play Store reviews are locale-scoped (like the App Store is per-country),
# so we sweep a small set of relevant (language, country) pairs. Add more
# here if reviews from other locales matter to you.
LOCALES = [("en", "np"), ("ne", "np")]

PAGE_SIZE = 200               # Google Play's per-request max
MAX_PAGES_PER_LOCALE = 250    # safety cap (up to ~50,000 reviews/locale) for a first backfill run
EARLY_STOP_AFTER_SEEN = 40    # consecutive already-known reviews -> assume we've caught up
REQUEST_DELAY = 1.0           # seconds between requests, to stay polite to Google's servers
MAX_RETRIES = 3               # retries per request before giving up on that page
RETRY_BACKOFF = 3             # seconds, doubles each retry

FIELDNAMES = ["locale", "review_id", "author", "rating", "review_text", "date", "app_version"]

# All paths are relative to the repo root, since the GitHub Actions
# workflow runs this script with the repo root as the working directory.
DATA_DIR = os.path.join("data", "android")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
MONTHLY_DIR = os.path.join(DATA_DIR, "monthly")
SEEN_IDS_PATH = os.path.join(DATA_DIR, "seen_ids.json")
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")


def call_with_retries(fn, *args, **kwargs):
    """Retry a flaky call a few times with backoff before giving up."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == MAX_RETRIES:
                raise
            print(f"      retry {attempt}/{MAX_RETRIES} after {type(exc).__name__}: {exc} "
                  f"(waiting {RETRY_BACKOFF * attempt}s)")
            time.sleep(RETRY_BACKOFF * attempt)


def fetch_reviews_for_locale(lang: str, country: str, is_known):
    """Fetch newest-first reviews for one (lang, country) locale, stopping
    early once we've clearly caught up with reviews we already know about
    (persisted from a previous run, or seen earlier in this same run)."""
    out = []
    token = None
    consecutive_seen = 0

    for page in range(1, MAX_PAGES_PER_LOCALE + 1):
        try:
            if token is None:
                result, token = call_with_retries(
                    reviews, APP_ID, lang=lang, country=country,
                    sort=Sort.NEWEST, count=PAGE_SIZE,
                )
            else:
                result, token = call_with_retries(reviews, APP_ID, continuation_token=token)
        except Exception as exc:
            print(f"    [{lang}-{country} p{page}] gave up after {MAX_RETRIES} retries "
                  f"({type(exc).__name__}: {exc}), stopping this locale.")
            break

        if not result:
            print(f"    [{lang}-{country} p{page}] no more reviews.")
            break

        page_new = 0
        stop_now = False
        for r in result:
            review_id = r.get("reviewId") or ""
            at = r.get("at")
            # google-play-scraper returns a naive datetime built from a Unix
            # timestamp via the runner's local system time. GitHub-hosted
            # runners default to UTC, so we treat it as UTC -- if you ever
            # run this locally on a machine in a different timezone, the
            # dates may be off by a few hours near midnight.
            date_label = at.isoformat() if hasattr(at, "isoformat") else str(at or "")
            key = review_id or f'{r.get("userName", "")}|{date_label}|{(r.get("content") or "")[:40]}'

            if is_known(key):
                consecutive_seen += 1
                if consecutive_seen >= EARLY_STOP_AFTER_SEEN:
                    stop_now = True
                    break
                continue

            consecutive_seen = 0
            out.append({
                "_key": key,
                "locale": f"{lang}-{country}",
                "review_id": review_id,
                "author": r.get("userName", ""),
                "rating": r.get("score", ""),
                "review_text": (r.get("content") or "").replace("\n", " ").strip(),
                "date": date_label,
                "app_version": r.get("reviewCreatedVersion") or r.get("appVersion") or "",
            })
            page_new += 1

        print(f"    [{lang}-{country} p{page}] {len(result)} fetched, {page_new} new")

        if stop_now:
            print(f"    [{lang}-{country}] caught up with known reviews, stopping this locale.")
            break
        if not token:
            break
        time.sleep(REQUEST_DELAY)

    return out


def parse_review_date_npt(date_label: str):
    """Convert a review's posted timestamp to an NPT calendar date string
    (YYYY-MM-DD). Returns None if it can't be parsed."""
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
    """Days from day 1 of the current NPT month through yesterday that
    don't have a data/android/daily/<date>.csv file yet."""
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

    # Pass 1: fetch everything, dedupe against the persisted ledger AND
    # against duplicates of the same review turning up under more than one
    # locale in this same run. Nothing is added to the persisted ledger
    # yet -- that only happens for reviews we actually decide to commit.
    candidates = []
    candidate_keys_this_run = set()

    def is_known(key):
        return key in seen_ids or key in candidate_keys_this_run

    for i, (lang, country) in enumerate(LOCALES, 1):
        print(f"[{i}/{len(LOCALES)}] Fetching reviews for locale '{lang}-{country}'...")
        locale_reviews = fetch_reviews_for_locale(lang, country, is_known)
        for r in locale_reviews:
            candidate_keys_this_run.add(r["_key"])
        candidates.extend(locale_reviews)
        print(f"    -> {len(locale_reviews)} new this locale")

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
        print("This can still self-heal on a later run if those reviews haven't been paginated")
        print("past yet -- or it may genuinely mean zero reviews that day.")
    else:
        print("\nNo gaps: every day this month (through yesterday) has at least one file.")


if __name__ == "__main__":
    main()