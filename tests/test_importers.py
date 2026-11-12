"""Characterization tests for the importers.

FIT uses the synthetic captures in tests/data/ (a constant-pace fake run and
a gym session — like everything under examples/, generated data, not real
recordings).
"""

from pathlib import Path

from fit import importers


def test_import_fit_generated_fixture():
    fixture = Path(__file__).parent / "data" / "test_run.fit"
    activity = importers.import_fit(str(fixture))
    assert activity == {
        "id": "2024-05-12T07:30:00",
        "type": "run",
        "date": "2024-05-12",
        "distance_km": 5.8,
        "duration_seconds": 1740,
        "source": "garmin",
        "elevation_gain_m": 42,
        "avg_heart_rate": 152,
        "max_heart_rate": 171,
        "avg_power": 260,
        "splits": {"5k_seconds": 1500.0},
    }


def test_import_fit_strength_session():
    """tests/data/test_strength.fit: sport 10 / sub_sport 20, HR-only records,
    and sets covering rest intervals, an unmapped category and a bodyweight
    set."""
    fixture = Path(__file__).parent / "data" / "test_strength.fit"
    activity = importers.import_fit(str(fixture), max_heart_rate=185)

    assert activity["type"] == "strength"
    assert activity["duration_seconds"] == 2700
    # No splits or power curve: neither means anything without a distance or
    # power stream. HR zones still land, from records carrying nothing else.
    assert "splits" not in activity
    assert "best_power" not in activity
    assert sum(activity["hr_zones"].values()) > 0

    assert [e["name"] for e in activity["exercises"]] == [
        "deadlift",
        "squat",
        "bench_press",
        "unknown_4242",
    ]
    deadlift = activity["exercises"][0]["sets"]
    # Rest sets dropped: three working sets plus the heavy single, not seven.
    assert deadlift == [
        {"reps": 10, "weight_kg": 100.0},
        {"reps": 10, "weight_kg": 100.0},
        {"reps": 10, "weight_kg": 100.0},
        {"reps": 3, "weight_kg": 130.0},
    ]


def test_fit_exercise_name_handles_every_category_shape():
    # A scalar string (fitparse's scalar-field path) is taken as-is; a real
    # watch's array of raw uint16s is unwrapped and resolved via the enum map;
    # an unmapped code (and FIT's 65534 sentinel) stays distinguishable.
    assert importers._fit_exercise_name("DEADLIFT") == "deadlift"
    assert importers._fit_exercise_name(["squat"]) == "squat"
    assert importers._fit_exercise_name([28]) == "squat"
    assert importers._fit_exercise_name(28) == "squat"
    assert importers._fit_exercise_name([0]) == "bench_press"
    assert importers._fit_exercise_name([65534]) == "unknown_65534"
    assert importers._fit_exercise_name(4242) == "unknown_4242"
    assert importers._fit_exercise_name(None) == "unknown"


def _garmin_set(category, reps, grams, set_type="ACTIVE"):
    exercises = [{"category": category, "name": "BACK_SQUATS"}] if category else []
    return {
        "setType": set_type,
        "repetitionCount": reps,
        "weight": grams,
        "exercises": exercises,
    }


def test_garmin_exercise_sets_correct_names_without_losing_weights():
    # The real shape this exists for: the watch left two sets of a squat ramp
    # as the 65534 sentinel and guessed pull_up for a bench press, all fixed in
    # the Connect app -- where Garmin reports the corrected names but a null
    # weight for the sets it holds no load for.
    activity = {
        "type": "strength",
        "exercises": [
            {"name": "squat", "sets": [{"reps": 5, "weight_kg": 20.0}]},
            {"name": "unknown_65534", "sets": [{"reps": 3, "weight_kg": 70.0}]},
            {"name": "pull_up", "sets": [{"reps": 12, "weight_kg": 24.0}]},
            {"name": "bench_press", "sets": [{"reps": 10, "weight_kg": 28.0}]},
        ],
    }
    garmin_sets = [
        _garmin_set("SQUAT", 5, None),
        _garmin_set(None, 60, None, set_type="REST"),
        _garmin_set("SQUAT", 3, None),
        _garmin_set("BENCH_PRESS", 12, 24000.0),
        _garmin_set("BENCH_PRESS", 10, 28000.0),
    ]

    merged = importers.apply_garmin_exercise_sets(activity, garmin_sets)["exercises"]

    # Rest sets ignored; the renamed sets group with their neighbours.
    assert [e["name"] for e in merged] == ["squat", "bench_press"]
    # The FIT weight survives where Garmin reports null.
    assert merged[0]["sets"] == [
        {"reps": 5, "weight_kg": 20.0},
        {"reps": 3, "weight_kg": 70.0},
    ]
    assert merged[1]["sets"] == [
        {"reps": 12, "weight_kg": 24.0},
        {"reps": 10, "weight_kg": 28.0},
    ]


def test_garmin_exercise_sets_abandon_a_merge_they_cannot_align():
    # A pairing that isn't set-for-set would rename sets to whatever sat at
    # that index, so a differing count or a disagreeing rep count leaves the
    # FIT-derived names alone rather than guessing.
    activity = {
        "type": "strength",
        "exercises": [{"name": "squat", "sets": [{"reps": 5, "weight_kg": 60.0}]}],
    }
    original = [dict(e) for e in activity["exercises"]]

    too_many = [
        _garmin_set("BENCH_PRESS", 5, None),
        _garmin_set("BENCH_PRESS", 5, None),
    ]
    assert (
        importers.apply_garmin_exercise_sets(activity, too_many)["exercises"]
        == original
    )

    wrong_reps = [_garmin_set("BENCH_PRESS", 8, None)]
    assert (
        importers.apply_garmin_exercise_sets(activity, wrong_reps)["exercises"]
        == original
    )

    assert importers.apply_garmin_exercise_sets(activity, [])["exercises"] == original


# --- power windows -------------------------------------------------------------
#
# The FIT fixture carries only distance and timestamp per record, and the repo
# deliberately ships no FIT-generation tooling, so these exercise the attach
# logic over a synthetic point stream. The records-to-stream mapping itself was
# verified against a real power-meter ride: 4,658 samples, best 20min 141W
# against a stored avg_power of 128W.


def _stream(samples, distance=True):
    return [
        {
            "elapsed_seconds": i,
            "distance_km": (i * 0.005) if distance else None,
            "hr": 140,
            "power": w,
        }
        for i, w in enumerate(samples)
    ]


def test_attach_best_power_stores_only_the_windows_the_ride_covers():
    """A ten-minute ride has a 1min and 5min figure but no 20min one, and a
    ride with no power at all gets no key rather than an empty dict."""
    assert importers._attach_best_power({}, _stream([200] * 2000))["best_power"] == {
        "1min": 200,
        "5min": 200,
        "20min": 200,
    }
    assert set(
        importers._attach_best_power({}, _stream([200] * 600))["best_power"]
    ) == {
        "1min",
        "5min",
    }
    no_power = [{"elapsed_seconds": i, "distance_km": i * 0.005} for i in range(2000)]
    assert "best_power" not in importers._attach_best_power({}, no_power)


def test_an_indoor_ride_with_no_distance_still_yields_power():
    """Points without distance are meaningful to power and HR even though a
    split cannot use them — an indoor trainer is exactly where an FTP test
    happens."""
    stream = _stream([250] * 2000, distance=False)
    assert importers._attach_best_power({}, stream)["best_power"]["20min"] == 250
    # ...and the split path drops them rather than raising on a None distance.
    assert importers._compute_splits("cycle", stream) == {}
