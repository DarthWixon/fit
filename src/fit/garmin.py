"""Garmin Connect API boundary. Returns raw bytes/dicts only — never fit's
activity shape, never the data dir.

Token lives at ~/.garminconnect, outside the data dir, so a zipped backup of
~/.fit carries no login. garminconnect is imported lazily (optional extra).
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
    """Resume the saved session, else prompt for credentials (and MFA) and save
    one. Raises GarminAuthError on failure."""
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


def get_exercise_sets(client, garmin_activity_id) -> list[dict]:
    """Raw server-side set records for one strength activity: the Connect-app
    corrections the original FIT export never carries. [] if Garmin has none."""
    response = client.get_activity_exercise_sets(garmin_activity_id)
    return response.get("exerciseSets", []) if response else []


def push_workout(client, workout_payload: dict) -> dict:
    """Upload one planner-built payload. Raw response contains "workoutId"."""
    return client.upload_workout(workout_payload)


def get_workout(client, workout_id) -> dict:
    """Fetch one workout back. Diffing this against what was pushed is how
    planner.py's schema notes get verified (scripts/diff_workout.py)."""
    return client.get_workout_by_id(workout_id)


def schedule_workout(client, workout_id, date_str: str) -> dict:
    """Place one pushed workout on one date. Atomic by design: a multi-week
    plan calls this per session. date_str must be pre-validated by the caller
    (planner.parse_schedule_date) — no date logic here."""
    return client.schedule_workout(workout_id, date_str)


def unschedule_workout(client, scheduled_workout_id):
    """Takes the *schedule* id from schedule_workout, not the workout id — one
    workout can sit on several dates."""
    return client.unschedule_workout(scheduled_workout_id)


def list_recent_activities(client, start_date: date, end_date: date) -> list[dict]:
    """Raw activity summaries in range — not yet fit's activity shape."""
    return client.get_activities_by_date(start_date.isoformat(), end_date.isoformat())


def download_activity_fit(client, garmin_activity_id) -> bytes:
    """Raw FIT bytes. Garmin's "original" export zips the .fit, so unwrap."""
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
