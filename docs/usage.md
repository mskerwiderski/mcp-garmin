# Usage examples

All examples below are made up but structurally real: the field names and units
are exactly what the tools return.

## Activities

> **Show me my last five runs.**

The model calls `list_activities(sport="running", limit=5)` and gets one compact
row per activity:

```json
{
  "activity_id": 24114943735,
  "name": "Morning Run",
  "start_local": "2026-08-25 07:12:04",
  "sport": "running",
  "distance_km": 12.404,
  "duration_min": 58.4,
  "moving_min": 57.1,
  "avg_hr": 142,
  "max_hr": 171,
  "avg_speed_ms": 3.54,
  "elevation_gain_m": 118,
  "calories": 812
}
```

Useful variations:

> Everything I did in July, grouped by sport.

> My cycling activities between 2026-06-01 and 2026-06-30.

> How much did I swim last month compared to the month before?

Sport keys are Garmin's own: `running`, `cycling`, `lap_swimming`,
`open_water_swimming`, `strength_training`, `hiking`, `walking`,
`multi_sport`. Dates are plain calendar days, `YYYY-MM-DD`.

## One activity in depth

> **Tell me everything about activity 24114943735.**

`get_activity` returns Garmin's own record: names, times, averages, training
effect, elevation, assigned gear.

> **Was the elevation on that ride real, or is that the barometer drifting?**

This is where `analyze_activity_fit` comes in. It downloads the original FIT and
reports what the **device** wrote, which is not always what Garmin's servers
computed:

```json
{
  "sport": "cycling",
  "sub_sport": "road",
  "start_time": "2026-08-23 07:11:32",
  "duration_s": 12482,
  "elapsed_s": 12630,
  "distance_m": 108420.5,
  "avg_hr": 131,
  "max_hr": 168,
  "avg_power": 187,
  "elevation_gain_m": 1284,
  "device": "Garmin Fenix 8 Pro"
}
```

Compare the two when a number looks wrong. Garmin applies server-side elevation
correction; the file has the barometric original.

## Time series

> **Plot my heart rate and power for that ride.**

`get_activity_streams` returns a downsampled series - 120 points by default, at
most 500 - with min, max and average per channel:

```json
{
  "n_source_points": 12482,
  "n_points": 120,
  "available_channels": ["hr", "power", "speed", "cadence", "altitude", "temperature"],
  "t_s": [0, 104, 208, "..."],
  "channels": {
    "hr":    {"values": [96, 118, 131, "..."], "min": 84, "max": 168, "avg": 131.4},
    "power": {"values": [0, 165, 210, "..."],  "min": 0,  "max": 642, "avg": 187.2}
  },
  "laps": ["..."],
  "legs": ["..."]
}
```

Ask for `include_gps` if you want the route, and name the channels you care
about to keep the answer small:

> Just the power and cadence curve, 60 points is enough.

For a multisport file, `legs` splits swim, bike and run with their own
distances, durations and averages.

## Did my sensor actually work?

> **Did the Stryd record power on Tuesday's run?**

`get_activity_sensors` lists the Connect-IQ fields in the file, grouped by the
app that wrote them, with a sample value and a flag for fields that stayed empty
or constant:

```json
[
  {
    "app": "Stryd",
    "fields": [
      {"name": "Form Power", "units": "W", "n": 3241, "sample": 61.0, "looks_empty": false},
      {"name": "Leg Spring Stiffness", "units": "kN/m", "n": 3241, "sample": 9.8, "looks_empty": false}
    ]
  }
]
```

`looks_empty: true` means the field is there but never carried real data -
exactly what you see when a sensor was not paired that day.

## Pool swims

> **Break down Tuesday's pool session by interval.**

`get_swim_detail` gives the pool length, every single length with stroke and
pace per 100 m, and the active intervals with stroke counts. Good for questions
like "did my pace fall off in the last set?" or "how many strokes per length in
the fast 50s?".

## Health and recovery

> **Why is my training readiness so low today?**

`get_daily_health` merges four Garmin endpoints into one answer:

```json
{
  "day": "2026-08-25",
  "steps": 6160,
  "resting_hr": 48,
  "stress_avg": 25,
  "body_battery_high": 51,
  "body_battery_low": 5,
  "sleep": {
    "sleep_h": 7.02,
    "deep_min": 93.0,
    "light_min": 262.0,
    "rem_min": 66.0,
    "awake_min": 26.0,
    "score": 79,
    "score_label": "FAIR"
  },
  "hrv_last_night": 26,
  "hrv_status": "BALANCED",
  "hrv_baseline_low": 25,
  "hrv_baseline_high": 33,
  "training_readiness": {
    "score": 2,
    "level": "POOR",
    "feedback": "LET_YOUR_BODY_RECOVER",
    "recovery_time_h": 60.8,
    "acute_load": 821,
    "is_morning_value": true
  }
}
```

Two things worth knowing when you read this:

- **Sleep is filed under the morning you woke up**, not the evening you went to
  bed.
- **`is_morning_value: false`** means the watch had not synced yet when Garmin
  computed readiness, so the number is stale rather than wrong.

More:

> How did my resting heart rate and HRV develop over the past two weeks?

> Compare my sleep on the nights before hard sessions with the rest of the week.

> What is my training status and where is my load focus?

## Trends over a period

> **How did my resting heart rate and Body Battery develop this month?**

`get_health_trend` answers a whole month in three requests, one row per day:

```json
[
  {"day": "2026-08-20", "steps": 11429, "step_goal": 10000, "distance_km": 9.099,
   "body_battery_charged": 52, "body_battery_drained": 47},
  {"day": "2026-08-21", "steps": 16416, "distance_km": 14.831,
   "body_battery_charged": 48, "body_battery_drained": 50, "vo2max_running": 50.9}
]
```

Ask `get_daily_health` for one specific day when you need sleep, HRV, stress or
readiness - Garmin has no range endpoint for those, so a week of them is seven
calls and worth asking for deliberately.

## The plan, not just the record

> **What is scheduled for the next two weeks?**

> **Which planned sessions did I skip in July?**

`get_calendar` returns what was planned - workouts from a training plan and
events such as races:

```json
[
  {"type": "workout", "id": 77, "title": "4x1000m", "date": "2026-09-02",
   "sport": "running", "planned_duration_min": 60.0, "training_plan_id": 42},
  {"type": "event", "id": 28616365, "title": "IRONMAN Vichy", "date": "2026-08-23",
   "start_time": "07:00", "timezone": "Europe/Paris", "is_race": true,
   "target": "226.0 kilometer"}
]
```

Combined with `list_activities` this answers the comparison question directly:
what was due, what actually happened, what is missing. `list_planned_workouts`
adds the written content of a session - the intervals, the rests, the notes.

## Where you stand

> **What does Garmin think I could run right now?**

`get_fitness_metrics`:

```json
{
  "race_predictions": {"5k": "22:54", "10k": "48:25",
                       "half_marathon": "1:47:53", "marathon": "3:54:39"},
  "fitness_age": 49.0, "chronological_age": 58, "achievable_fitness_age": 50.7,
  "endurance_score": 7497, "hill_score": 38, "vo2max": 50.5,
  "lifetime": {"activities": 6607, "distance_km": 129757.9, "hours": 7111}
}
```

> **What are my personal records, and when did I set them?**

`list_personal_records` returns each record with the activity behind it, so the
follow-up question - "what did that race look like?" - is one more call away.

## Zones and weather

`get_activity` now carries the time per heart rate zone, the same for power
where a meter was present, and the weather at the time:

```json
{
  "hr_zones": [{"zone": 2, "from_bpm": 96, "minutes": 41.3},
               {"zone": 3, "from_bpm": 112, "minutes": 12.8}],
  "weather": {"temperature_c": 15.0, "humidity_pct": 94, "wind_kmh": 16.1,
              "wind_from": "n", "conditions": "Showers"}
}
```

That makes fair comparisons possible: "was I slower because it was hotter, or
because I was tired?" is answerable when both sides are in the data.

## Challenges

> **Where do I stand in the running challenge?**

`list_challenges` shows the challenges you run against friends, newest first and
including the one currently in progress:

```json
{"challenge_id": "A8638E...", "name": "Lauf-Challenge", "sport": "running",
 "state": "running", "start": "2026-08-01", "end": "2026-08-31", "my_rank": 5}
```

`get_challenge` with that id returns the table: every player with rank, total
distance and last sync, and your own row marked with `is_you`.

> How many kilometres behind fourth place am I, and how many days are left?

## Questions that chain several tools

The interesting ones. The model works these out by combining calls:

> **Compare this month's running volume with last month's.**
> Two `list_activities` calls, summed.

> **Which of my shoes is closest to its replacement distance?**
> `list_gear` - it reports `pct_of_limit` directly where a limit is set, and
> `activities`, `used_km`, `used_hours` and `days_used` where it is not.

> **Was Sunday's race actually run at my threshold?**
> `get_profile` for the threshold heart rate, `get_activity_streams` for the
> heart rate curve.

> **Did I recover from the race, and what does the trend say?**
> `get_health_trend` over the past weeks plus `get_training_status`.

> **Did I train what my plan asked for last month?**
> `get_calendar` for what was due, `list_activities` for what happened.

## Practical tips

**Name the connector when it does not fire.** "Using my Garmin data, ..." is
enough.

**Ask for less when answers get long.** Streams accept `max_points` and a
channel filter; activity lists accept `limit`, capped at 100.

**The first FIT call for an activity is the slow one.** The file is downloaded
once, then cached, so follow-up questions about the same activity are fast.

**When something looks empty, ask for `whoami`.** It reports which Garmin
account is connected and how long the token is valid - that separates "no data"
from "not connected".

## What it will not do

- **No writes.** It cannot rename an activity, upload a file, change gear
  assignments or delete anything in Garmin Connect.
- **No other people's data.** On a hosted instance every account sees only its
  own, and the challenge leaderboards show only what Garmin already shows you.
- **No coaching claims.** It reports what your device and Garmin recorded.
  Whether that means you should train today is your call, and your coach's.
