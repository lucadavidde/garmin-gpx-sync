# garmin-gpx-sync

Downloads your Garmin Connect activities as GPX files, on a schedule, without
re-downloading anything you already have — and uploads each one straight to
Google Drive.

## How it works

There's no official Garmin API for personal use, so this uses
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect), a
Python wrapper that logs in the same way the Garmin Connect website/app does
and caches a session token afterwards. This script wraps that library to:

1. Log in (using a cached token after the first run, so no credentials are
   needed for scheduled runs).
2. Fetch your recent activities.
3. Download any activity not already downloaded, as GPX, into `gpx/`.
4. Upload each newly downloaded GPX file to Google Drive via the Drive API,
   and record its Drive link. See [Google Drive setup](#google-drive-setup).
5. Keep a small `status_excerpt.json` — the most recent activities from
   `status.json`, capped at 10KB — updated in place on Drive after every
   run. See [Status excerpt](#status-excerpt).
6. Optionally copy each new GPX file into another local directory of your
   choice too.
7. Remember which activities it has already downloaded, so the next run only
   fetches what's new.

Because this relies on an unofficial/reverse-engineered login flow rather
than a supported API, it can break if Garmin changes their login process —
see [Troubleshooting](#troubleshooting) if that happens. (The Google Drive
side uses Google's official, supported API.)

## Files in this project

| Path                     | Purpose                                                             |
|--------------------------|----------------------------------------------------------------------|
| `sync_garmin.py`         | The script itself.                                                  |
| `run.sh`                 | Wrapper for cron — activates the venv and forwards any arguments.   |
| `requirements.txt`       | Python dependencies (`garminconnect`, Google API client libraries). |
| `venv/`                  | Isolated virtualenv (not portable between machines — see Setup).    |
| `gpx/`                   | Every downloaded GPX file lands here. This is the local source of truth, independent of Drive upload success. |
| `.garmin_tokens/`        | Cached Garmin login session. Delete this to force a fresh login.    |
| `gdrive_credentials.json`| OAuth client ID downloaded from Google Cloud Console (you provide this — not committed to git). |
| `.gdrive_token.json`     | Cached Google Drive authorization. Delete this to force re-consent. |
| `state.json`             | List of activity IDs already downloaded (used to skip re-downloads).|
| `status.json`            | Human-readable manifest of every downloaded activity, including its Drive link, sorted by activity date (not download date). See [Status file](#status-file). |
| `status_excerpt.json`    | The most recent activities from `status.json`, capped at 10KB, also kept updated on Drive. See [Status excerpt](#status-excerpt). |
| `.gdrive_status_excerpt_id` | Cached Drive file ID for `status_excerpt.json`, so it's updated in place on Drive instead of re-created every run. |
| `sync.log`               | Timestamped log of every run (also mirrored to the console when run interactively). |

## Setup

```bash
cd ~/garmin-gpx-sync   # or wherever you cloned/copied this
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

The `venv/` folder contains absolute paths baked in at creation time, so if
you copy this project to a different machine, delete and rebuild `venv/`
there rather than copying it:

```bash
rm -rf venv
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## First run (manual, interactive)

The very first run must be done by hand, in a real terminal, because it
needs to prompt for your Garmin Connect email/password (and an MFA code, if
you have that enabled on your account):

```bash
./venv/bin/python sync_garmin.py
```

On success it caches your session to `.garmin_tokens/` and downloads your
recent activities into `gpx/`. Every run after this reuses that cached
session — no credentials needed again unless the token expires or is deleted.

The first run will also open a browser window for Google Drive consent (see
[Google Drive setup](#google-drive-setup) below) — complete that setup
first, otherwise the script exits before downloading anything.

## Google Drive setup

Uploads use the official Google Drive API with the narrow `drive.file`
scope, which only lets the app see files *it* creates — not your whole
Drive.

1. In the [Google Cloud Console](https://console.cloud.google.com/), create
   a project (or reuse one) and enable the **Google Drive API** under
   "APIs & Services" → "Library".
2. Under "APIs & Services" → "Credentials", click "Create Credentials" →
   "OAuth client ID". If prompted, configure the OAuth consent screen first
   (choose "External" and add your own Google account as a test user, unless
   you have a Workspace account).
3. For the client ID's application type, choose **Desktop app**.
4. Download the resulting JSON file, and save it in this project's root as
   `gdrive_credentials.json`.
5. (Optional) If you want uploads to land in a specific folder instead of
   the root of "My Drive", open that folder in Drive, copy the ID from its
   URL (`https://drive.google.com/drive/folders/<FOLDER_ID>`), and set it as
   an environment variable before running (e.g. add
   `export GDRIVE_FOLDER_ID=<FOLDER_ID>` to your shell profile, or prefix
   the cron command with it).

The first run (see above) opens a browser to complete the consent flow and
caches the resulting token to `.gdrive_token.json`; subsequent runs (manual
or cron) reuse it silently, refreshing it automatically when it expires.

**Running the first-run consent over SSH:** `flow.run_local_server()` needs
a browser reachable from the machine running the script. If you're setting
this up on a headless server over SSH, either run the first manual login on
your local machine and copy `.gdrive_token.json` (and
`gdrive_credentials.json`) over to the server afterwards, or forward the
port it listens on (e.g. `ssh -L 8080:localhost:8080 your-server`) and open
the printed URL in your local browser.

If you'd rather skip Drive entirely for a given run (e.g. while testing),
pass `--no-gdrive` — GPX files are still downloaded and saved locally, just
without a `gdriveLink`.

### One-time full history backfill

By default each run only checks your 100 most-recent activities (enough for
routine incremental syncing). If you have more than ~100 activities and want
everything, run once with `--all`, which pages through your entire history:

```bash
./venv/bin/python sync_garmin.py --all
```

You don't need `--all` again afterwards — the normal 100-most-recent check
is enough to catch anything new since the last run.

### Copying GPX files elsewhere

`gpx/` inside this project is always the canonical copy (it's what the
"already downloaded" tracking is based on). If you also want copies in
another folder — e.g. synced to cloud storage, or organized alongside other
training data — pass `--copy-to`:

```bash
./venv/bin/python sync_garmin.py --copy-to ~/Documents/GPX
```

The destination directory is created automatically if it doesn't exist.
This can be combined with `--all` for the initial backfill.

### Status file

`status.json` is a manifest of every activity that has ever been downloaded,
rewritten in full after each run and always sorted by the activity's own
date (`startTimeGMT`) — not by when it happened to be downloaded. Each entry:

```json
{
  "activityId": 24114635005,
  "activityName": "Roma - Ripetizioni 10 X 2 min - 10 x 1",
  "startTimeGMT": "2026-08-25 16:58:20",
  "filename": "2026-08-25_16-58-20_24114635005_Roma_-_Ripetizioni_10_X_2_min_-_10_x_1.gpx",
  "gdriveLink": "https://drive.google.com/file/d/1a2B3c.../view"
}
```

`gdriveLink` is added once the file has been successfully uploaded to Drive.
It's absent for entries from a run that used `--no-gdrive`, or if the
upload failed (check `sync.log`) — in either case the next run without
`--no-gdrive` will retry the upload for any entry still missing a link, as
long as the file is still present in `gpx/`.

This is meant to be read, not relied on for correctness — `state.json` is
what the script actually checks to decide what's new. If you ever manually
edit or delete entries in `state.json`, `status.json` won't automatically
match afterwards; it's rebuilt incrementally as activities are downloaded,
not derived fresh from `gpx/` on each run.

### Status excerpt

`status_excerpt.json` is a trimmed, newest-first copy of `status.json`,
capped at 10KB (`STATUS_EXCERPT_MAX_BYTES` in `sync_garmin.py`) — as many of
your most recent activities as fit in that budget, same entry format as
`status.json`. It's built by reading `status.json` right after it's
written, not from a separate in-memory copy, so it's always exactly
consistent with what's on disk. It's written locally on every run, and —
as long as Drive uploads are enabled — the same file is created once on
Drive and then *updated in place* on every subsequent run (its Drive file
ID is cached in `.gdrive_status_excerpt_id`), so you always have one
small, current file to check recent activity from without pulling the full
manifest or any GPX files. It's regenerated even on runs with no new
activities, so it stays in sync if `status.json` was backfilled (e.g. new
`gdriveLink`s added).

## Scheduling with cron

Once the first manual login above has succeeded, schedule recurring runs:

```bash
crontab -e
```

Add a line like this (adjust the time/frequency and path to taste):

```cron
7 6 * * * /path/to/garmin-gpx-sync/run.sh --copy-to "/path/to/destination"
```

This example runs once daily at 6:07am. `run.sh` activates the venv and
forwards any arguments straight through to `sync_garmin.py`, so `--copy-to`
and `--all` both work the same way as running it by hand.

**macOS note:** recent macOS versions require granting `cron` (or
`/usr/sbin/cron`) **Full Disk Access** under System Settings → Privacy &
Security, or scheduled runs may silently fail to access files in your home
directory.

Check `sync.log` after a scheduled run to confirm it fired and see what (if
anything) it downloaded.

## Troubleshooting

**Cron run logs "No valid cached session and not running interactively"**
The cached token in `.garmin_tokens/` is missing or expired, and the script
refuses to hang waiting for a password prompt from a non-interactive cron
job. Fix it by rerunning the manual login once:
```bash
./venv/bin/python sync_garmin.py
```

**`AttributeError` mentioning `garth` or `client` during login**
The `garminconnect` library has changed its internal API across versions
before (0.2.x vs 0.3.x rename `self.garth` to `self.client`). The script
already handles both known variants automatically; if a future release
changes it again, the fix is to check the installed version's source
(`./venv/bin/pip show garminconnect` and inspect `garminconnect/__init__.py`
in `site-packages`) and adjust the `login()` function's fallback logic in
`sync_garmin.py` accordingly.

**MFA / two-factor prompts**
Handled automatically during the interactive first-run login — you'll be
prompted for the code in the terminal.

**Cron run logs "No valid cached Google Drive token and not running interactively"**
Same idea as the Garmin equivalent above: `.gdrive_token.json` is missing,
expired, or its refresh token was revoked (e.g. you removed the app's
access under your Google Account's third-party access settings). Fix by
rerunning the manual login once to redo the consent flow, or add
`--no-gdrive` to the cron command if you want to disable Drive uploads.

**`FileNotFoundError` or login error mentioning `gdrive_credentials.json`**
You haven't downloaded the OAuth client ID from Google Cloud Console yet, or
saved it under a different name/location. See
[Google Drive setup](#google-drive-setup).

**Some entries in `status.json` have no `gdriveLink`**
Either they were downloaded with `--no-gdrive`, or the upload failed for
that file (check `sync.log` for the specific error — a common cause is the
Drive API not being enabled on your Google Cloud project, or the daily
upload quota being hit). Just run again without `--no-gdrive`: any entry
still missing a link, whose GPX file is still in `gpx/`, gets its upload
retried automatically.

**Activities are missing from before a certain date**
See [One-time full history backfill](#one-time-full-history-backfill) above
— by default only the 100 most-recent activities are checked per run.

**Nothing downloads but no errors either**
Check `state.json` — if an activity ID is already listed there, it's treated
as already downloaded and skipped even if the file is missing from `gpx/`.
Remove its ID from `state.json` (or delete the whole file to reset tracking)
to force it to be re-downloaded.

**Resetting tracking to force a full re-download**
Delete `state.json` and `status.json` outright rather than emptying them to
zero bytes — an empty file is treated the same as "no state" (so this is
safe), but do it with `rm`, not by truncating to blank in an editor, just to
avoid confusion about what state the files are in. Then run again (with
`--all` if you want the full history re-downloaded, not just the most
recent window).

**The same activities get re-downloaded on every run**
`state.json` should grow to include every activity ID ever downloaded and
never lose track of them. If you see this, check the size of the
`seen_ids` list in `state.json` — it should keep growing over time and
never shrink. (An earlier version of this script capped it at 500 entries
using `list[-500:]`, which silently dropped a pseudo-random subset of IDs
each run because `seen_ids` is a Python `set` — sets have no meaningful
order, so "last 500" wasn't "500 most recent." That cap has been removed;
the file is tiny even with thousands of entries, so there's no need to cap
it.)
