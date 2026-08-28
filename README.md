# garmin-gpx-sync

Downloads your Garmin Connect activities as GPX files, on a schedule, without
re-downloading anything you already have.

## How it works

There's no official Garmin API for personal use, so this uses
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect), a
Python wrapper that logs in the same way the Garmin Connect website/app does
and caches a session token afterwards. This script wraps that library to:

1. Log in (using a cached token after the first run, so no credentials are
   needed for scheduled runs).
2. Fetch your recent activities.
3. Download any activity not already downloaded, as GPX, into `gpx/`.
4. Optionally copy each new GPX file into another directory of your choice.
5. Remember which activities it has already downloaded, so the next run only
   fetches what's new.

Because this relies on an unofficial/reverse-engineered login flow rather
than a supported API, it can break if Garmin changes their login process —
see [Troubleshooting](#troubleshooting) if that happens.

## Files in this project

| Path                | Purpose                                                             |
|---------------------|----------------------------------------------------------------------|
| `sync_garmin.py`    | The script itself.                                                  |
| `run.sh`            | Wrapper for cron — activates the venv and forwards any arguments.   |
| `requirements.txt`  | Python dependencies (`garminconnect`).                              |
| `venv/`             | Isolated virtualenv (not portable between machines — see Setup).    |
| `gpx/`              | Every downloaded GPX file lands here. This is the source of truth. |
| `.garmin_tokens/`   | Cached login session. Delete this to force a fresh login.           |
| `state.json`        | List of activity IDs already downloaded (used to skip re-downloads).|
| `sync.log`          | Timestamped log of every run (also mirrored to the console when run interactively). |

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

**Activities are missing from before a certain date**
See [One-time full history backfill](#one-time-full-history-backfill) above
— by default only the 100 most-recent activities are checked per run.

**Nothing downloads but no errors either**
Check `state.json` — if an activity ID is already listed there, it's treated
as already downloaded and skipped even if the file is missing from `gpx/`.
Remove its ID from `state.json` (or delete the whole file to reset tracking)
to force it to be re-downloaded.
