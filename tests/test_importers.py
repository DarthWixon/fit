"""Characterization tests for the importers.

GPX/TCX use small inline fixture strings; FIT uses the synthetic capture in
tests/data/test_run.fit (a constant-pace fake run — like everything under
examples/, generated data, not a real recording). Latitude steps of 0.009
degrees are ~1.0008km apart by haversine, which keeps expected distances easy
to reason about.
"""

from pathlib import Path

import pytest

from fit import importers

GPX_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
  <metadata><time>2024-01-15T08:30:00Z</time></metadata>
  <trk>
    <type>running</type>
    <trkseg>
      <trkpt lat="0.000" lon="0.0">
        <ele>10</ele><time>2024-01-15T08:30:00Z</time>
        <extensions>
          <gpxtpx:TrackPointExtension xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
            <gpxtpx:hr>140</gpxtpx:hr>
          </gpxtpx:TrackPointExtension>
        </extensions>
      </trkpt>
      <trkpt lat="0.009" lon="0.0">
        <ele>20</ele><time>2024-01-15T08:35:00Z</time>
        <extensions>
          <gpxtpx:TrackPointExtension xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
            <gpxtpx:hr>150</gpxtpx:hr>
          </gpxtpx:TrackPointExtension>
        </extensions>
      </trkpt>
      <trkpt lat="0.018" lon="0.0">
        <ele>15</ele><time>2024-01-15T08:40:00Z</time>
        <extensions>
          <gpxtpx:TrackPointExtension xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
            <gpxtpx:hr>160</gpxtpx:hr>
          </gpxtpx:TrackPointExtension>
        </extensions>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""

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
          </Trackpoint>
          <Trackpoint>
            <Time>2024-01-15T08:35:00Z</Time>
            <Position><LatitudeDegrees>0.009</LatitudeDegrees><LongitudeDegrees>0.0</LongitudeDegrees></Position>
            <AltitudeMeters>20</AltitudeMeters>
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
          </Trackpoint>
          <Trackpoint>
            <Time>2024-01-15T08:40:00Z</Time>
            <Position><LatitudeDegrees>0.018</LatitudeDegrees><LongitudeDegrees>0.0</LongitudeDegrees></Position>
            <AltitudeMeters>30</AltitudeMeters>
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
)


def test_import_gpx(tmp_path):
    path = tmp_path / "run.gpx"
    path.write_text(GPX_FIXTURE)
    activity = importers.import_gpx(str(path))

    assert activity["id"] == "2024-01-15T08:30:00"
    assert activity["type"] == "run"
    assert activity["date"] == "2024-01-15"
    assert activity["distance_km"] == pytest.approx(2.0, abs=0.01)
    assert activity["duration_seconds"] == 600
    assert activity["elevation_gain_m"] == 10        # 10->20 up, 20->15 down
    assert activity["avg_heart_rate"] == 150
    assert activity["max_heart_rate"] == 160
    assert activity["source"] == "gpx"
    assert "splits" not in activity                  # 2km run, no 5k to find


def test_import_tcx(tmp_path):
    path = tmp_path / "run.tcx"
    path.write_text(TCX_FIXTURE)
    activity = importers.import_tcx(str(path))

    assert activity["id"] == "2024-01-15T08:30:00"
    assert activity["type"] == "run"
    assert activity["date"] == "2024-01-15"
    assert activity["distance_km"] == 2.0            # lap DistanceMeters summed
    assert activity["duration_seconds"] == 600       # lap TotalTimeSeconds summed
    assert activity["elevation_gain_m"] == 15        # per-lap altitude reset
    assert activity["avg_heart_rate"] == 145         # mean of lap averages
    assert activity["max_heart_rate"] == 165
    assert activity["avg_power"] == 200              # mean of lap AvgWatts
    assert activity["source"] == "garmin"
    assert "splits" not in activity


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


def test_import_strava_csv_maps_and_drops_types(tmp_path):
    path = tmp_path / "activities.csv"
    path.write_text(STRAVA_CSV_FIXTURE)
    activities, warnings = importers.import_strava_csv(str(path))

    assert len(activities) == 1                      # Workout row dropped
    assert activities[0] == {
        "id": "2024-01-15T08:30:00",
        "type": "run",
        "date": "2024-01-15",
        "distance_km": 10.2,
        "duration_seconds": 3120,
        "source": "strava",
    }

    # The dropped Workout row is reported, not silent.
    assert len(warnings) == 1
    assert "Workout" in warnings[0]
