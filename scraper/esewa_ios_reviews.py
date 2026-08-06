"""
eSewa iOS App Store Review Scraper — incremental / repo-friendly version
-------------------------------------------------------------------------
Pulls customer ratings & reviews for the eSewa app from Apple's public
RSS review feed, across every App Store country storefront, and writes
them into a repo-friendly, date-partitioned layout:

    data/daily/<YYYY-MM-DD>.csv     -> only reviews first seen on this run
    data/monthly/<YYYY-MM>.csv      -> running append-only log for the month
    data/seen_ids.json              -> dedupe ledger (persisted across runs)
    data/manifest.json              -> index of what files exist + last run info

This is designed to run once a day inside GitHub Actions, where the
runner is thrown away after every job. The "state" that lets us know
which reviews are new is `data/seen_ids.json`, which is committed back
to the repo at the end of each run (see .github/workflows/scrape.yml).

No API key needed — uses Apple's free public RSS feed:
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
from datetime import datetime, timezone

import requests

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


def write_csv(path: str, rows: list):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def update_manifest(run_date: str, month_key: str, new_count: int, total_seen: int):
    manifest = {"daily_files": [], "monthly_files": []}
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            try:
                manifest = json.load(f)
            except json.JSONDecodeError:
                pass

    daily_files = sorted(set(manifest.get("daily_files", []) + [f"daily/{run_date}.csv"]))
    monthly_files = sorted(set(manifest.get("monthly_files", []) + [f"monthly/{month_key}.csv"]))

    manifest = {
        "last_run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_run_new_reviews": new_count,
        "total_reviews_seen": total_seen,
        "daily_files": daily_files,
        "monthly_files": monthly_files,
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def main():
    os.makedirs(DAILY_DIR, exist_ok=True)
    os.makedirs(MONTHLY_DIR, exist_ok=True)

    seen_ids = load_seen_ids()
    starting_seen_count = len(seen_ids)

    new_reviews = []

    for i, country in enumerate(COUNTRIES, 1):
        print(f"[{i}/{len(COUNTRIES)}] Fetching reviews for '{country}'...")
        country_reviews = fetch_reviews_for_country(country)

        new_count = 0
        for r in country_reviews:
            # Dedupe: same review can appear under multiple storefronts,
            # and the same reviews will reappear every run since the feed
            # always returns the most recent ~500 reviews per storefront.
            key = r["review_id"] or f'{r["author"]}|{r["title"]}|{r["date"]}'
            if key in seen_ids:
                continue
            seen_ids.add(key)
            new_reviews.append(r)
            new_count += 1

        print(f"    -> {len(country_reviews)} found, {new_count} new")

    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")

    daily_path = os.path.join(DAILY_DIR, f"{run_date}.csv")
    monthly_path = os.path.join(MONTHLY_DIR, f"{month_key}.csv")

    # Daily file: exactly what was new on this run (a fresh file each day).
    write_csv(daily_path, new_reviews)

    # Monthly file: append-only running log for the month.
    append_csv(monthly_path, new_reviews)

    save_seen_ids(seen_ids)
    update_manifest(run_date, month_key, len(new_reviews), len(seen_ids))

    print(f"\nDone. {len(new_reviews)} new reviews this run "
          f"(seen ledger grew from {starting_seen_count} to {len(seen_ids)}).")
    print(f"Daily file:   {daily_path}")
    print(f"Monthly file: {monthly_path}")


if __name__ == "__main__":
    main()
