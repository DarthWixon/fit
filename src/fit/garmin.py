"""Garmin Connect API boundary. The only module that talks to the Garmin
Connect network API (via the optional `garminconnect` package). Returns raw
FIT bytes and raw Garmin activity summaries - never builds fit's activity
dict shape and never touches the data dir; cli.py feeds the bytes through
importers.import_fit -> storage.write_activity like every other import path.

The session token lives at the library-default ~/.garminconnect, deliberately
outside the data dir so ~/.fit stays credential-free (a zipped backup never
carries a login). `import garminconnect` happens lazily inside functions
(same pattern as importers.import_fit's lazy fitparse import) so every other
command works without the optional dependency installed.
"""

import io
import zipfile
from datetime import date

TOKEN_STORE = "~/.garminconnect"

INSTALL_HINT = (
    "fit garmin-sync needs the optional 'garminconnect' dependency.\n"
    "Install it with: pip install -e '.[garmin]'"
)


class GarminAuthError(Exception):
    pass


def _garminconnect():
    try:
        import garminconnect
    except ImportError as exc:
        raise GarminAuthError(INSTALL_HINT) from exc
    return garminconnect


def login():
    """Resumes a saved session from TOKEN_STORE if present; otherwise prompts
    for email/password (and an MFA code, if the account needs it) via typer,
    then saves a session token to TOKEN_STORE for next time (the library
    persists it itself after a credential login). Raises GarminAuthError on
    failure. Returns a logged-in garminconnect.Garmin client."""
    import typer

    gc = _garminconnect()

    try:
        client = gc.Garmin()
        client.login(TOKEN_STORE)
        return client
    except (gc.GarminConnectAuthenticationError, FileNotFoundError):
        pass  # no usable saved session - fall through to credential login

    email = typer.prompt("Garmin Connect email")
    password = typer.prompt("Garmin Connect password", hide_input=True)
    try:
        client = gc.Garmin(
            email=email,
            password=password,
            prompt_mfa=lambda: typer.prompt("MFA code").strip(),
        )
        client.login(TOKEN_STORE)
    except gc.GarminConnectAuthenticationError as exc:
        raise GarminAuthError(f"Garmin Connect login failed: {exc}") from exc
    return client


def push_workout(client, workout_payload: dict) -> dict:
    """Upload one workout-service payload to Garmin Connect. Returns the raw
    response dict (contains "workoutId"). The payload is built by planner.py
    — this module never shapes workout dicts itself."""
    return client.upload_workout(workout_payload)


def list_recent_activities(client, start_date: date, end_date: date) -> list[dict]:
    """Raw Garmin Connect activity summaries (activityId, activityType,
    startTimeLocal, ...) in [start_date, end_date] - NOT yet in fit's
    activity dict shape."""
    return client.get_activities_by_date(start_date.isoformat(), end_date.isoformat())


def download_activity_fit(client, garmin_activity_id) -> bytes:
    """Raw FIT bytes for one activity. Garmin's "original" export wraps the
    .fit in a zip, so unwrap it here (same unwrap-then-hand-over shape as
    importers._import_strava_linked_file uses for gzipped files)."""
    gc = _garminconnect()
    raw = client.download_activity(
        garmin_activity_id, dl_fmt=gc.Garmin.ActivityDownloadFormat.ORIGINAL
    )
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not names:
                raise ValueError(
                    f"no .fit file inside Garmin download for activity {garmin_activity_id}"
                )
            return zf.read(names[0])
    return raw
