#!/usr/bin/env python3
"""Download new Garmin Connect activities as GPX and/or FIT files and upload
them to Google Drive.

First run must be done manually (interactively) to log in and cache a
Garmin session token, and to complete the Google Drive OAuth consent:

    ./venv/bin/python sync_garmin.py --format gpx

After that, this script reuses the cached Garmin token and Drive
credentials and can be run unattended from cron. If either cached
credential is missing or expired when run non-interactively, it logs
an error and exits instead of hanging on a login prompt.
"""
import argparse
import io
import json
import logging
import os
import re
import shutil
import sys
import zipfile
from getpass import getpass
from pathlib import Path

from garminconnect import Garmin
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

BASE_DIR = Path(__file__).resolve().parent
TOKEN_DIR = BASE_DIR / ".garmin_tokens"
STATUS_FILE = BASE_DIR / "status.json"
LOG_FILE = BASE_DIR / "sync.log"

FORMAT_DIRS = {"gpx": BASE_DIR / "gpx", "fit": BASE_DIR / "fit"}
FORMAT_MIMETYPES = {"gpx": "application/gpx+xml", "fit": "application/octet-stream"}

# Google Drive OAuth client (downloaded from Google Cloud Console) and the
# token cached after the first interactive consent. See the "Google Drive
# setup" section of the README.
GDRIVE_CREDENTIALS_FILE = BASE_DIR / "gdrive_credentials.json"
GDRIVE_TOKEN_FILE = BASE_DIR / ".gdrive_token.json"
GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# Optional: ID of the Drive folder to upload into (the part after
# /folders/ in the folder's URL). If unset, files go to "My Drive" root.
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")

# A small, most-recent-first excerpt of status.json, kept under this size and
# re-uploaded (in place, same Drive file) after every run — handy for
# glancing at recent activity from somewhere that doesn't want to fetch the
# full manifest or the GPX/FIT files themselves. Only ever exists on Drive,
# not written to local disk.
STATUS_EXCERPT_MAX_BYTES = 10 * 1024
GDRIVE_STATUS_EXCERPT_NAME = "status_excerpt.json"
GDRIVE_STATUS_EXCERPT_ID_FILE = BASE_DIR / ".gdrive_status_excerpt_id"

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
        description="Download new Garmin Connect activities as GPX and/or FIT files."
    )
    parser.add_argument(
        "--format",
        choices=["gpx", "fit", "both"],
        required=True,
        help=(
            "Which file format(s) to download. Required — there is no "
            "default, so an existing scheduled run must be updated to pass "
            "this explicitly rather than silently changing what it fetches."
        ),
    )
    parser.add_argument(
        "--copy-to",
        type=Path,
        default=None,
        help="Additional directory to copy each newly downloaded file into.",
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
            "Skip uploading to Google Drive entirely (files are still saved "
            "locally, just without a gdriveLink in status.json)."
        ),
    )
    return parser.parse_args()


def load_status():
    """Load status.json into a dict keyed by activityId, migrating legacy
    entries (from before FIT support existed) in place: the old singular
    filename/gdriveLink fields were always GPX, so they become
    gpx_filename/gpx_gdriveLink."""
    if not STATUS_FILE.exists():
        return {}
    text = STATUS_FILE.read_text().strip()
    if not text:
        return {}

    status_by_id = {}
    for entry in json.loads(text):
        if "filename" in entry and "gpx_filename" not in entry:
            entry["gpx_filename"] = entry.pop("filename")
        if "gdriveLink" in entry and "gpx_gdriveLink" not in entry:
            entry["gpx_gdriveLink"] = entry.pop("gdriveLink")
        status_by_id[entry["activityId"]] = entry
    return status_by_id


def save_status(status_by_id):
    entries = sorted(status_by_id.values(), key=lambda e: e["startTimeGMT"])
    STATUS_FILE.write_text(json.dumps(entries, indent=2))


def build_status_excerpt(max_bytes=STATUS_EXCERPT_MAX_BYTES):
    """Read the already-written status.json and return the most recent
    activities (newest first) as indented JSON, keeping only as many as
    fit within max_bytes."""
    if not STATUS_FILE.exists():
        return json.dumps([], indent=2)
    text = STATUS_FILE.read_text().strip()
    entries = json.loads(text) if text else []

    newest_first = sorted(entries, key=lambda e: e["startTimeGMT"], reverse=True)
    kept = []
    for entry in newest_first:
        candidate = kept + [entry]
        serialized = json.dumps(candidate, indent=2)
        if len(serialized.encode("utf-8")) > max_bytes:
            break
        kept = candidate
    return json.dumps(kept, indent=2)


def upload_status_excerpt(service, excerpt_json):
    """Create or update the status.json excerpt on Drive. Reuses the same
    Drive file across runs (its ID is cached locally) so this updates one
    file in place instead of creating a new one every time."""
    media = MediaIoBaseUpload(
        io.BytesIO(excerpt_json.encode("utf-8")), mimetype="application/json", resumable=False
    )

    file_id = None
    if GDRIVE_STATUS_EXCERPT_ID_FILE.exists():
        file_id = GDRIVE_STATUS_EXCERPT_ID_FILE.read_text().strip() or None

    if file_id:
        try:
            # A file "deleted" from the Drive UI is normally just moved to
            # Trash, not actually removed — its ID stays valid, so a plain
            # update() would silently succeed against the trashed copy
            # instead of surfacing a 404. Check for that explicitly and
            # treat a trashed (or genuinely missing) file the same way:
            # fall through and create a fresh one.
            existing = service.files().get(fileId=file_id, fields="id, trashed").execute()
            if existing.get("trashed"):
                log.warning("Cached status excerpt Drive file is in Trash; creating a new one.")
                file_id = None
        except HttpError as exc:
            if exc.resp.status == 404:
                log.warning("Cached status excerpt Drive file no longer exists; creating a new one.")
                file_id = None
            else:
                raise

    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
        return file_id

    metadata = {"name": GDRIVE_STATUS_EXCERPT_NAME}
    if GDRIVE_FOLDER_ID:
        metadata["parents"] = [GDRIVE_FOLDER_ID]
    uploaded = service.files().create(body=metadata, media_body=media, fields="id").execute()
    file_id = uploaded["id"]
    GDRIVE_STATUS_EXCERPT_ID_FILE.write_text(file_id)
    return file_id


def finalize_status(status_by_id, gdrive_service):
    """Persist status.json, then — when Drive uploads are enabled — build
    the status excerpt straight from the just-written status.json and push
    it to Drive too. Called at every exit point of main() so the excerpt
    always reflects the latest run, even one that found no new activities."""
    save_status(status_by_id)
    if gdrive_service is None:
        return
    excerpt_json = build_status_excerpt()
    try:
        upload_status_excerpt(gdrive_service, excerpt_json)
        log.info("  updated status excerpt on Google Drive.")
    except HttpError as exc:
        log.error("  failed to upload status excerpt to Google Drive: %s", exc)


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
            "Run 'venv/bin/python sync_garmin.py --format gpx' by hand once to log in."
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
                "Run 'venv/bin/python sync_garmin.py --format gpx' by hand once to authorize "
                "Drive access (or pass --no-gdrive to skip uploads for this run)."
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


def upload_to_gdrive(service, path, filename, mimetype):
    """Upload a single file to Drive and return a shareable link."""
    metadata = {"name": filename}
    if GDRIVE_FOLDER_ID:
        metadata["parents"] = [GDRIVE_FOLDER_ID]
    media = MediaFileUpload(str(path), mimetype=mimetype, resumable=False)
    uploaded = (
        service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    return uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{uploaded['id']}/view"


def slugify(name):
    name = re.sub(r"[^\w\-]+", "_", name or "").strip("_")
    return name or "activity"


def activity_filename(activity, ext):
    activity_id = activity["activityId"]
    name = slugify(activity.get("activityName"))
    start = activity["startTimeGMT"].replace(" ", "_").replace(":", "-")
    return f"{start}_{activity_id}_{name}.{ext}"


def extract_fit_entries(zip_bytes):
    """Garmin's "original" download format is a zip wrapping the device's
    raw .fit file(s) — normally exactly one, but multi-sport activities can
    contain more than one. Return their raw bytes, sorted by member name for
    determinism."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = sorted(n for n in zf.namelist() if n.lower().endswith(".fit"))
        return [zf.read(n) for n in members]


def download_format(client, activity_id, fmt):
    """Return a list of raw file payloads for one activity/format. GPX is
    always a single payload; FIT can be more than one for multi-sport
    activities (see extract_fit_entries)."""
    if fmt == "gpx":
        return [client.download_activity(activity_id, dl_fmt=client.ActivityDownloadFormat.GPX)]
    zip_bytes = client.download_activity(activity_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL)
    fit_payloads = extract_fit_entries(zip_bytes)
    if not fit_payloads:
        raise ValueError("original download did not contain any .fit file")
    return fit_payloads


def format_missing(entry, fmt):
    return not entry.get(f"{fmt}_filename") and not entry.get(f"{fmt}_filenames")


def save_format_download(entry, fmt, activity, payloads):
    """Write downloaded file(s) for one format to disk and record their
    filename(s) on the status entry — a single string field for the normal
    single-file case, a list field for the rare multi-file case. Returns the
    (filename, path) pairs written, for Drive upload / --copy-to."""
    format_dir = FORMAT_DIRS[fmt]
    if len(payloads) == 1:
        filename = activity_filename(activity, fmt)
        out_path = format_dir / filename
        out_path.write_bytes(payloads[0])
        entry[f"{fmt}_filename"] = filename
        return [(filename, out_path)]

    log.warning(
        "  %s download for activity %s contained %d files (multi-sport?); saving all.",
        fmt, activity["activityId"], len(payloads),
    )
    base = activity_filename(activity, fmt)
    stem = base[: -(len(fmt) + 1)]
    written = []
    filenames = []
    for idx, data in enumerate(payloads, start=1):
        filename = f"{stem}_part{idx}.{fmt}"
        out_path = format_dir / filename
        out_path.write_bytes(data)
        filenames.append(filename)
        written.append((filename, out_path))
    entry[f"{fmt}_filenames"] = filenames
    return written


def upload_format_files(gdrive_service, entry, fmt, written):
    links = []
    for filename, path in written:
        try:
            link = upload_to_gdrive(gdrive_service, path, filename, FORMAT_MIMETYPES[fmt])
            log.info("  uploaded to Google Drive: %s", link)
            links.append(link)
        except HttpError as exc:
            log.error("  failed to upload %s to Google Drive: %s", filename, exc)

    if len(written) == 1:
        if links:
            entry[f"{fmt}_gdriveLink"] = links[0]
    elif links:
        entry[f"{fmt}_gdriveLinks"] = links


def backfill_gdrive_links(gdrive_service, status_by_id, requested):
    """Upload any already-downloaded file for a requested format that's
    still missing a Drive link — e.g. downloaded with --no-gdrive, or a
    prior upload failed. For the rare multi-file case, retries the whole
    set together rather than tracking partial success per file."""
    for entry in status_by_id.values():
        for fmt in requested:
            filename = entry.get(f"{fmt}_filename")
            if filename and not entry.get(f"{fmt}_gdriveLink"):
                local_path = FORMAT_DIRS[fmt] / filename
                if local_path.exists():
                    try:
                        entry[f"{fmt}_gdriveLink"] = upload_to_gdrive(
                            gdrive_service, local_path, filename, FORMAT_MIMETYPES[fmt]
                        )
                        log.info("  backfilled Drive upload for %s", filename)
                    except HttpError as exc:
                        log.error("  failed to backfill Drive upload for %s: %s", filename, exc)
                continue

            filenames = entry.get(f"{fmt}_filenames")
            if filenames and not entry.get(f"{fmt}_gdriveLinks"):
                links = []
                for fn in filenames:
                    local_path = FORMAT_DIRS[fmt] / fn
                    if not local_path.exists():
                        continue
                    try:
                        links.append(upload_to_gdrive(gdrive_service, local_path, fn, FORMAT_MIMETYPES[fmt]))
                        log.info("  backfilled Drive upload for %s", fn)
                    except HttpError as exc:
                        log.error("  failed to backfill Drive upload for %s: %s", fn, exc)
                if links:
                    entry[f"{fmt}_gdriveLinks"] = links


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
    requested = {"gpx", "fit"} if args.format == "both" else {args.format}

    copy_to = args.copy_to
    if copy_to is not None:
        copy_to.mkdir(parents=True, exist_ok=True)

    for fmt in requested:
        FORMAT_DIRS[fmt].mkdir(parents=True, exist_ok=True)

    status_by_id = load_status()

    client = login()
    gdrive_service = None if args.no_gdrive else gdrive_login()

    if args.all:
        log.info("Paging through your entire activity history...")
    else:
        log.info("Checking the %d most recent activities for new ones...", RECENT_ACTIVITIES_TO_CHECK)
    activities = fetch_activities(client, args.all)

    if gdrive_service is not None:
        backfill_gdrive_links(gdrive_service, status_by_id, requested)

    def missing_formats(activity):
        entry = status_by_id.get(activity["activityId"], {})
        return [fmt for fmt in requested if format_missing(entry, fmt)]

    new_activities = sorted(
        (a for a in activities if missing_formats(a)),
        key=lambda a: a["startTimeGMT"],
    )

    if not new_activities:
        log.info("No new activities.")
        finalize_status(status_by_id, gdrive_service)
        return

    total = len(new_activities)
    log.info("Found %d activit%s needing a download.", total, "y" if total == 1 else "ies")

    for i, activity in enumerate(new_activities, start=1):
        activity_id = activity["activityId"]
        entry = status_by_id.setdefault(
            activity_id,
            {
                "activityId": activity_id,
                "activityName": activity.get("activityName"),
                "startTimeGMT": activity["startTimeGMT"],
            },
        )
        start = activity["startTimeGMT"].replace(" ", "_").replace(":", "-")
        missing = missing_formats(activity)

        log.info(
            "[%d/%d] Downloading '%s' (%s) as %s...",
            i, total, activity.get("activityName"), start, "+".join(sorted(missing)),
        )

        copy_candidates = []
        for fmt in missing:
            try:
                payloads = download_format(client, activity_id, fmt)
            except Exception as exc:
                log.error("Failed to download %s for activity %s: %s", fmt, activity_id, exc)
                continue

            written = save_format_download(entry, fmt, activity, payloads)
            for filename, out_path in written:
                log.info("  saved to %s", out_path)
            copy_candidates.extend(written)

            if gdrive_service is not None:
                upload_format_files(gdrive_service, entry, fmt, written)

        if copy_to is not None:
            for filename, out_path in copy_candidates:
                try:
                    shutil.copy2(out_path, copy_to / filename)
                    log.info("  copied to %s", copy_to / filename)
                except OSError as exc:
                    log.error("  failed to copy to %s: %s", copy_to, exc)

    finalize_status(status_by_id, gdrive_service)


if __name__ == "__main__":
    main()
