#!/usr/bin/env python3
"""Download new Garmin Connect activities as GPX files and upload them to
Google Drive.

First run must be done manually (interactively) to log in and cache a
Garmin session token, and to complete the Google Drive OAuth consent:

    ./venv/bin/python sync_garmin.py

After that, this script reuses the cached Garmin token and Drive
credentials and can be run unattended from cron. If either cached
credential is missing or expired when run non-interactively, it logs
an error and exits instead of hanging on a login prompt.
"""
import argparse
import json
import logging
import os
import re
import shutil
import sys
from getpass import getpass
from pathlib import Path

from garminconnect import Garmin
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

BASE_DIR = Path(__file__).resolve().parent
TOKEN_DIR = BASE_DIR / ".garmin_tokens"
GPX_DIR = BASE_DIR / "gpx"
STATE_FILE = BASE_DIR / "state.json"
STATUS_FILE = BASE_DIR / "status.json"
LOG_FILE = BASE_DIR / "sync.log"

# Google Drive OAuth client (downloaded from Google Cloud Console) and the
# token cached after the first interactive consent. See the "Google Drive
# setup" section of the README.
GDRIVE_CREDENTIALS_FILE = BASE_DIR / "gdrive_credentials.json"
GDRIVE_TOKEN_FILE = BASE_DIR / ".gdrive_token.json"
GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# Optional: ID of the Drive folder to upload into (the part after
# /folders/ in the folder's URL). If unset, files go to "My Drive" root.
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")

# How many of the most recent activities to check each run. Must be
# comfortably larger than the number of activities you'd log between
# scheduled runs.
RECENT_ACTIVITIES_TO_CHECK = 100

handlers = [logging.FileHandler(LOG_FILE)]
if sys.stdout.isatty():
    # Mirror progress to the console for interactive runs; cron runs only
    # get the file so scheduled output stays quiet unless there's an error.
    handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=handlers,
)
log = logging.getLogger("garmin-gpx-sync")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download new Garmin Connect activities as GPX files."
    )
    parser.add_argument(
        "--copy-to",
        type=Path,
        default=None,
        help="Additional directory to copy each newly downloaded GPX file into.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Page through your entire activity history instead of only the "
            f"{RECENT_ACTIVITIES_TO_CHECK} most recent activities. Use this "
            "once for an initial backfill; not needed for routine runs."
        ),
    )
    parser.add_argument(
        "--no-gdrive",
        action="store_true",
        help=(
            "Skip uploading to Google Drive entirely (GPX files are still "
            "saved locally to gpx/, just without a gdriveLink in status.json)."
        ),
    )
    return parser.parse_args()


def load_state():
    if STATE_FILE.exists():
        text = STATE_FILE.read_text().strip()
        if text:
            return json.loads(text)
    return {"seen_ids": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_status():
    if STATUS_FILE.exists():
        text = STATUS_FILE.read_text().strip()
        if text:
            return {entry["activityId"]: entry for entry in json.loads(text)}
    return {}


def save_status(status_by_id):
    entries = sorted(status_by_id.values(), key=lambda e: e["startTimeGMT"])
    STATUS_FILE.write_text(json.dumps(entries, indent=2))


def login():
    if TOKEN_DIR.exists() and any(TOKEN_DIR.iterdir()):
        client = Garmin()
        try:
            client.login(str(TOKEN_DIR))
            return client
        except Exception as exc:
            log.warning("Cached token invalid/expired (%s); need to log in again.", exc)

    if not sys.stdin.isatty():
        log.error(
            "No valid cached session and not running interactively. "
            "Run 'venv/bin/python sync_garmin.py' by hand once to log in."
        )
        sys.exit(1)

    email = input("Garmin Connect email: ").strip()
    password = getpass("Garmin Connect password: ")
    client = Garmin(email=email, password=password)

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # garminconnect >= 0.3: falls back to credentials and auto-persists
        # to tokenstore if no cached token is found there.
        client.login(str(TOKEN_DIR))
    except Exception:
        # garminconnect <= 0.2.x: a tokenstore arg only ever loads (no
        # credential fallback), so log in with credentials directly.
        client.login()

    # Persist tokens ourselves too. The internal auth-client attribute was
    # renamed across versions (garth -> client), and older versions don't
    # auto-persist on credential login, so do this defensively either way.
    auth_backend = getattr(client, "client", None) or getattr(client, "garth", None)
    if auth_backend is not None and hasattr(auth_backend, "dump"):
        try:
            auth_backend.dump(str(TOKEN_DIR))
        except Exception as exc:
            log.warning("Could not explicitly persist token (may already be saved): %s", exc)

    print(f"Login cached to {TOKEN_DIR} for future unattended runs.")
    return client


def gdrive_login():
    """Return an authorized Drive API client, refreshing or creating the
    cached token as needed. Mirrors the Garmin login()'s
    interactive-first-run / cached-token-after pattern."""
    creds = None
    if GDRIVE_TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(GDRIVE_TOKEN_FILE), GDRIVE_SCOPES)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("Cached Google Drive token unreadable (%s); need to re-authorize.", exc)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            log.warning("Could not refresh Google Drive token (%s); need to re-authorize.", exc)
            creds = None

    if not creds or not creds.valid:
        if not sys.stdin.isatty():
            log.error(
                "No valid cached Google Drive token and not running interactively. "
                "Run 'venv/bin/python sync_garmin.py' by hand once to authorize Drive access "
                "(or pass --no-gdrive to skip uploads for this run)."
            )
            sys.exit(1)
        if not GDRIVE_CREDENTIALS_FILE.exists():
            log.error(
                "Missing %s. Download an OAuth client ID (Desktop app) from Google Cloud "
                "Console and save it there — see the README's Google Drive setup section.",
                GDRIVE_CREDENTIALS_FILE,
            )
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(str(GDRIVE_CREDENTIALS_FILE), GDRIVE_SCOPES)
        creds = flow.run_local_server(port=0)
        GDRIVE_TOKEN_FILE.write_text(creds.to_json())
        print(f"Google Drive authorization cached to {GDRIVE_TOKEN_FILE} for future unattended runs.")

    return build("drive", "v3", credentials=creds)


def upload_to_gdrive(service, path, filename):
    """Upload a single GPX file to Drive and return a shareable link."""
    metadata = {"name": filename}
    if GDRIVE_FOLDER_ID:
        metadata["parents"] = [GDRIVE_FOLDER_ID]
    media = MediaFileUpload(str(path), mimetype="application/gpx+xml", resumable=False)
    uploaded = (
        service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    return uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{uploaded['id']}/view"


def slugify(name):
    name = re.sub(r"[^\w\-]+", "_", name or "").strip("_")
    return name or "activity"


def gpx_filename(activity):
    activity_id = activity["activityId"]
    name = slugify(activity.get("activityName"))
    start = activity["startTimeGMT"].replace(" ", "_").replace(":", "-")
    return f"{start}_{activity_id}_{name}.gpx"


def fetch_activities(client, full_history):
    if not full_history:
        return client.get_activities(0, RECENT_ACTIVITIES_TO_CHECK)

    activities = []
    page_size = 100
    start = 0
    while True:
        page = client.get_activities(start, page_size)
        if not page:
            break
        activities.extend(page)
        log.info("  fetched %d activities so far...", len(activities))
        start += page_size
    return activities


def main():
    args = parse_args()
    copy_to = args.copy_to
    if copy_to is not None:
        copy_to.mkdir(parents=True, exist_ok=True)

    GPX_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    seen_ids = set(state["seen_ids"])
    status_by_id = load_status()

    client = login()
    gdrive_service = None if args.no_gdrive else gdrive_login()

    if args.all:
        log.info("Paging through your entire activity history...")
    else:
        log.info("Checking the %d most recent activities for new ones...", RECENT_ACTIVITIES_TO_CHECK)
    activities = fetch_activities(client, args.all)

    # Backfill manifest entries for activities that were already downloaded
    # (e.g. by a run before status.json existed, or outside the usual
    # RECENT_ACTIVITIES_TO_CHECK window) but never made it into status.json.
    for activity in activities:
        activity_id = activity["activityId"]
        if activity_id in status_by_id or activity_id not in seen_ids:
            continue
        filename = gpx_filename(activity)
        if (GPX_DIR / filename).exists():
            status_by_id[activity_id] = {
                "activityId": activity_id,
                "activityName": activity.get("activityName"),
                "startTimeGMT": activity["startTimeGMT"],
                "filename": filename,
            }

    # Backfill Drive links for entries that already exist in status.json
    # (or were just backfilled above) but don't have one yet — e.g. GPX
    # files downloaded before Drive upload support existed, or a prior run
    # where the upload failed.
    if gdrive_service is not None:
        for activity_id, entry in status_by_id.items():
            if entry.get("gdriveLink"):
                continue
            local_path = GPX_DIR / entry["filename"]
            if not local_path.exists():
                continue
            try:
                entry["gdriveLink"] = upload_to_gdrive(gdrive_service, local_path, entry["filename"])
                log.info("  backfilled Drive upload for %s", entry["filename"])
            except HttpError as exc:
                log.error("  failed to backfill Drive upload for %s: %s", entry["filename"], exc)

    new_activities = sorted(
        (a for a in activities if a["activityId"] not in seen_ids),
        key=lambda a: a["startTimeGMT"],
    )

    if not new_activities:
        log.info("No new activities.")
        save_status(status_by_id)
        return

    total = len(new_activities)
    log.info("Found %d new activit%s to download.", total, "y" if total == 1 else "ies")

    for i, activity in enumerate(new_activities, start=1):
        activity_id = activity["activityId"]
        filename = gpx_filename(activity)
        out_path = GPX_DIR / filename
        start = activity["startTimeGMT"].replace(" ", "_").replace(":", "-")

        log.info("[%d/%d] Downloading '%s' (%s)...", i, total, activity.get("activityName"), start)
        try:
            gpx_data = client.download_activity(
                activity_id, dl_fmt=client.ActivityDownloadFormat.GPX
            )
        except Exception as exc:
            log.error("Failed to download activity %s: %s", activity_id, exc)
            continue

        out_path.write_bytes(gpx_data)
        seen_ids.add(activity_id)
        log.info("  saved to %s", out_path)

        status_entry = {
            "activityId": activity_id,
            "activityName": activity.get("activityName"),
            "startTimeGMT": activity["startTimeGMT"],
            "filename": filename,
        }

        if gdrive_service is not None:
            try:
                status_entry["gdriveLink"] = upload_to_gdrive(gdrive_service, out_path, filename)
                log.info("  uploaded to Google Drive: %s", status_entry["gdriveLink"])
            except HttpError as exc:
                log.error("  failed to upload %s to Google Drive: %s", filename, exc)

        status_by_id[activity_id] = status_entry

        if copy_to is not None:
            try:
                shutil.copy2(out_path, copy_to / filename)
                log.info("  copied to %s", copy_to / filename)
            except OSError as exc:
                log.error("  failed to copy to %s: %s", copy_to, exc)

    state["seen_ids"] = list(seen_ids)
    save_state(state)
    save_status(status_by_id)


if __name__ == "__main__":
    main()
