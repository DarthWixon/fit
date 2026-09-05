"""Characterization tests for the importers.

TCX uses a small inline fixture string; FIT uses the synthetic capture in
tests/data/test_run.fit (a constant-pace fake run — like everything under
examples/, generated data, not a real recording). Latitude steps of 0.009
degrees are ~1.0008km apart by haversine, which keeps expected distances easy
to reason about.
"""

from pathlib import Path

import pytest

from fit import importers

# Leading whitespace before the declaration is deliberate: Strava pads its TCX
# exports this way and _parse_xml_root must tolerate it. Lap 2 starts at a
# higher altitude (25m) than lap 1 ends (20m): per-lap elevation reset gives
# gain 10 + 5 = 15, whereas carrying altitude across laps would give 20.
TCX_FIXTURE = """   <?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Running">
      <Id>2024-01-15T08:30:00Z</Id>
      <Lap StartTime="2024-01-15T08:30:00Z">
        <TotalTimeSeconds>300</TotalTimeSeconds>
        <DistanceMeters>1000</DistanceMeters>
        <AverageHeartRateBpm><Value>140</Value></AverageHeartRateBpm>
        <MaximumHeartRateBpm><Value>155</Value></MaximumHeartRateBpm>
        <Track>
          <Trackpoint>
            <Time>2024-01-15T08:30:00Z</Time>
            <Position><LatitudeDegrees>0.000</LatitudeDegrees><LongitudeDegrees>0.0</LongitudeDegrees></Position>
            <AltitudeMeters>10</AltitudeMeters>
            <HeartRateBpm><Value>100</Value></HeartRateBpm>
          </Trackpoint>
          <Trackpoint>
            <Time>2024-01-15T08:35:00Z</Time>
            <Position><LatitudeDegrees>0.009</LatitudeDegrees><LongitudeDegrees>0.0</LongitudeDegrees></Position>
            <AltitudeMeters>20</AltitudeMeters>
            <HeartRateBpm><Value>140</Value></HeartRateBpm>
          </Trackpoint>
        </Track>
        <Extensions>
          <ns3:LX xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
            <ns3:AvgWatts>190</ns3:AvgWatts>
          </ns3:LX>
        </Extensions>
      </Lap>
      <Lap StartTime="2024-01-15T08:35:00Z">
        <TotalTimeSeconds>300</TotalTimeSeconds>
        <DistanceMeters>1000</DistanceMeters>
        <AverageHeartRateBpm><Value>150</Value></AverageHeartRateBpm>
        <MaximumHeartRateBpm><Value>165</Value></MaximumHeartRateBpm>
        <Track>
          <Trackpoint>
            <Time>2024-01-15T08:35:10Z</Time>
            <Position><LatitudeDegrees>0.009</LatitudeDegrees><LongitudeDegrees>0.0</LongitudeDegrees></Position>
            <AltitudeMeters>25</AltitudeMeters>
            <HeartRateBpm><Value>150</Value></HeartRateBpm>
          </Trackpoint>
          <Trackpoint>
            <Time>2024-01-15T08:40:00Z</Time>
            <Position><LatitudeDegrees>0.018</LatitudeDegrees><LongitudeDegrees>0.0</LongitudeDegrees></Position>
            <AltitudeMeters>30</AltitudeMeters>
            <HeartRateBpm><Value>170</Value></HeartRateBpm>
          </Trackpoint>
        </Track>
        <Extensions>
          <ns3:LX xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
            <ns3:AvgWatts>210</ns3:AvgWatts>
          </ns3:LX>
        </Extensions>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>
"""

STRAVA_CSV_FIXTURE = (
    "Activity Date,Activity Type,Distance,Elapsed Time,Filename\n"
    '"Jan 15, 2024, 8:30:00 AM",Run,10200,3120,\n'
    '"Jan 16, 2024, 7:00:00 AM",Workout,0,1800,\n'
    '"Jan 17, 2024, 9:00:00 AM",Canoeing,8400,3600,\n'
    '"Jan 18, 2024, 9:00:00 AM",Kayaking,5000,2400,\n'
)


def test_import_tcx(tmp_path):
    path = tmp_path / "run.tcx"
    path.write_text(TCX_FIXTURE)
    activity = importers.import_tcx(str(path))

    assert activity["id"] == "2024-01-15T08:30:00"
    assert activity["type"] == "run"
    assert activity["date"] == "2024-01-15"
    assert activity["distance_km"] == 2.0  # lap DistanceMeters summed
    assert activity["duration_seconds"] == 600  # lap TotalTimeSeconds summed
    assert activity["elevation_gain_m"] == 15  # per-lap altitude reset
    assert activity["avg_heart_rate"] == 145  # mean of lap averages
    assert activity["max_heart_rate"] == 165
    assert activity["avg_power"] == 200  # mean of lap AvgWatts
    assert activity["source"] == "garmin"
    assert "splits" not in activity
    assert "hr_zones" not in activity  # max_heart_rate not passed, defaults to 0


def test_import_tcx_with_max_heart_rate(tmp_path):
    path = tmp_path / "run.tcx"
    path.write_text(TCX_FIXTURE)
    activity = importers.import_tcx(str(path), max_heart_rate=200)

    # Per-trackpoint HR: 100 (0->300s, zone1), 140 (300->310s, zone3),
    # 150 (310->600s, zone3), 170 (no following gap to attribute).
    assert activity["hr_zones"] == {
        "zone1_seconds": 300.0,
        "zone2_seconds": 0.0,
        "zone3_seconds": 300.0,
        "zone4_seconds": 0.0,
        "zone5_seconds": 0.0,
    }


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
    # fitparse decodes known categories itself; the FIT profile defines the
    # field as an array, and an unmapped code must stay distinguishable.
    assert importers._fit_exercise_name("DEADLIFT") == "deadlift"
    assert importers._fit_exercise_name(["squat"]) == "squat"
    assert importers._fit_exercise_name(4242) == "unknown_4242"
    assert importers._fit_exercise_name(None) == "unknown"


def test_import_strava_csv_maps_and_drops_types(tmp_path):
    path = tmp_path / "activities.csv"
    path.write_text(STRAVA_CSV_FIXTURE)
    activities, warnings = importers.import_strava_csv(str(path))

    assert len(activities) == 2  # Workout and Kayaking rows dropped
    assert activities[0] == {
        "id": "2024-01-15T08:30:00",
        "type": "run",
        "date": "2024-01-15",
        "distance_km": 10.2,
        "duration_seconds": 3120,
        "source": "strava",
    }
    # Canoeing maps to canoe; Kayaking deliberately does not (canoe-only scope).
    assert activities[1]["type"] == "canoe"
    assert activities[1]["distance_km"] == pytest.approx(8.4)

    # The dropped Workout and Kayaking rows are reported, not silent.
    assert len(warnings) == 1
    assert "Workout" in warnings[0]
    assert "Kayaking" in warnings[0]


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
