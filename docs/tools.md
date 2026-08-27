# Tool reference

Twenty tools, all read-only. Distances are metres or kilometres as named,
durations are seconds or minutes as named, dates are `YYYY-MM-DD` calendar days
in your local time.

Every response is projected: Garmin's raw fields are filtered down to the useful
ones, numbers are rounded, and empty values are dropped rather than sent as
`null`.

---

## Activities

### `list_activities(date_from=None, date_to=None, sport=None, limit=20)`

Activities newest first.

| Parameter | Meaning |
|---|---|
| `date_from`, `date_to` | calendar days, inclusive |
| `sport` | Garmin type key: `running`, `cycling`, `lap_swimming`, `open_water_swimming`, `strength_training`, `hiking`, `walking`, `multi_sport`, … |
| `limit` | 1 to 100, default 20 |

Returns per activity: `activity_id`, `name`, `start_local`, `sport`,
`distance_km`, `duration_min`, `moving_min`, `avg_hr`, `max_hr`, `avg_power`,
`avg_speed_ms`, `elevation_gain_m`, `calories`.

### `get_activity(activity_id)`

Garmin Connect's own record of one activity: name, description, sport, event
type, local and GMT start, timezone, distance, durations (moving, elapsed),
heart rate, power including normalized power, cadence, elevation gain and loss,
calories, aerobic and anaerobic training effect, temperatures, whether Garmin's
elevation correction is on, and assigned gear.

Adds three things Garmin keeps separately:

- `hr_zones` - minutes per heart rate zone, only zones with time in them
- `power_zones` - the same for power, present when the activity has it
- `weather` - `temperature_c`, `feels_like_c`, `dew_point_c`, `humidity_pct`,
  `wind_kmh`, `wind_from`, `conditions`. Garmin serves this endpoint in
  Fahrenheit and mph whatever your account says; the values here are converted.

All three are best effort: an activity recorded without a heart rate belt, a
power meter or a location simply has fewer of them.

These are the numbers Garmin computed on its servers. For what the watch
recorded, use `analyze_activity_fit`.

---

## The original FIT file

These four download the activity's original file. The first call fetches it, the
rest come from a local cache.

### `analyze_activity_fit(activity_id)`

Metrics as the device wrote them: `sport`, `sub_sport`, `start_time`,
`duration_s`, `elapsed_s`, `distance_m`, `avg_hr`, `max_hr`, `avg_power`,
`avg_cadence`, `avg_speed_ms`, `elevation_gain_m`, `calories`, `device`, and for
multisport files the individual legs.

Use it when Garmin's numbers look wrong, or when the question is about what was
actually measured.

### `get_activity_streams(activity_id, channels=None, max_points=120, include_gps=False)`

Time series from the file.

| Parameter | Meaning |
|---|---|
| `channels` | list of channel keys; omit for all |
| `max_points` | 10 to 500, default 120 |
| `include_gps` | adds the route polyline and bounding box |

Returns `n_source_points`, `n_points`, `available_channels`, `t_s` (seconds from
the start), `channels` (each with `values` plus `min`, `max`, `avg`), `laps` and
`legs`.

Channels depend on the file: `hr`, `power`, `speed`, `cadence`, `altitude`,
`temperature` are common, and any Connect-IQ field (Stryd, SmO2, CORE) appears
under its own name. Call once without `channels` to see what exists.

### `get_swim_detail(activity_id)`

Pool swims only: `pool_m`, every `length` with stroke, seconds and pace per
100 m, and the active `laps` with distance, stroke count and heart rate.

### `get_activity_sensors(activity_id)`

Connect-IQ / developer fields in the file, grouped by the app that wrote them.
Per field: `name`, `units`, `n` samples, a `sample` value and `looks_empty`,
which is true when the field never carried real data - the signature of a sensor
that was not paired.

---

## Health

### `get_daily_health(day=None)`

One day, four Garmin endpoints merged. Defaults to today.

Returns steps and step goal, distance, calories, resting, minimum and maximum
heart rate, average and maximum stress, Body Battery (high, low, charged,
drained), respiration, intensity minutes, floors, plus:

- `sleep`: `sleep_h`, `deep_min`, `light_min`, `rem_min`, `awake_min`,
  `avg_respiration`, `avg_spo2`, `score`, `score_label`
- `hrv_last_night`, `hrv_status`, `hrv_baseline_low`, `hrv_baseline_high`
- `training_readiness`: `score`, `level`, `feedback`, `sleep_score`,
  `recovery_time_h`, `hrv_factor`, `stress_history_factor`, `acute_load`,
  `is_morning_value`

Sleep belongs to the morning you woke up. `is_morning_value: false` means the
watch had not synced when Garmin computed readiness.

### `get_training_status(day=None)`

Garmin's aggregated status: the status phrase (`PRODUCTIVE`, `MAINTAINING`,
`OVERREACHING`, …), acute load and load ratio, the load focus split (low
aerobic, high aerobic, anaerobic) and VO2max for running and cycling.

### `get_body_composition(date_from, date_to)`

Per day: `weight_kg`, `body_fat_pct`, `muscle_mass_kg`, `bmi`, `source`.

### `get_blood_pressure(date_from, date_to)`

Per measurement: `measured_at`, `systolic`, `diastolic`, `pulse`, `note`.

---

## Trends and plan

### `get_health_trend(date_from, date_to)`

Daily values across a whole period in **three** requests instead of one per
day: `steps`, `step_goal`, `distance_km`, `body_battery_charged`,
`body_battery_drained`, `vo2max_running`, `vo2max_cycling`. One row per day,
days without data simply missing.

Use this for any question about a period. For sleep, HRV, stress and training
readiness of a single day, `get_daily_health` remains the right tool - those
have no range endpoint at Garmin.

The Body Battery response also carries the full intraday curve; it is dropped
here, because a month of curves is tens of thousands of numbers.

### `get_calendar(date_from, date_to)`

What was **planned**: scheduled workouts from a training plan and events such
as races. Per item: `type` (`workout` or `event`), `id`, `title`, `date`,
`sport`, `start_time`, `timezone`, `is_race`, `training_plan_id`,
`planned_duration_min`, `planned_distance_km`, `target`.

Accounts differ in what they schedule, so fields are optional throughout: an
account following a plan sees workouts, a racer sees events, an account with
neither gets an empty list. What actually happened is in `list_activities` -
the calendar's own activity, weight and blood pressure entries are filtered out
rather than duplicated.

Ranges longer than a year are refused; the underlying endpoint works per month.

### `list_planned_workouts(limit=20)`

The structured workouts stored in the account: `workout_id`, `name`, `sport`,
`description`, `estimated_duration_min`, `estimated_distance_km`, `updated`.

These are definitions, not appointments - `get_calendar` says when they are due.

---

## Fitness

### `get_fitness_metrics(day=None)`

Garmin's summary judgements in one call:

- `race_predictions` - `5k`, `10k`, `half_marathon`, `marathon` as `h:mm:ss`
- `fitness_age`, `chronological_age`, `achievable_fitness_age`
- `endurance_score`, `hill_score`, `hill_strength`, `hill_endurance`, `vo2max`
- `lifetime` - `activities`, `distance_km`, `hours`, `elevation_m`

### `list_personal_records()`

All-time records with the activity that set each one: `record` (a label such as
`5 km` or `longest ride`), `type_id`, `sport`, `date`, `activity_id`,
`activity_name`, and either `time` plus `time_s`, `distance_km` or
`elevation_m`.

Garmin identifies records by a numeric type only. The labels cover the common
running and cycling records; anything else - swimming records, for instance -
is returned with its raw `value` and `type_id` rather than a guessed label.

---

## Challenges

### `list_challenges(sport=None, limit=10)`

Social challenges against friends, newest first, including the one currently
running. `sport` filters on `running`, `cycling` or `swimming`.

Per challenge: `challenge_id`, `name`, `sport`, `state` (`running`, `finished`,
`upcoming`), `start`, `end`, `my_rank`, and `players` for finished ones - Garmin
reports no player count while a challenge is in progress.

### `get_challenge(challenge_id)`

The table: `players` and a `leaderboard` with `rank`, `name`, `total_km` and
`last_sync` per participant. Your own row carries `is_you: true`.

---

## Context

### `list_gear(include_retired=False)`

Gear with its usage: `uuid`, `name`, `type` (`SHOES`, `BIKE`), `brand`,
`status`, `activities`, `used_km`, `used_hours`, `days_used`, `first_use`.

Where a replacement limit is set, `limit_km` or `limit_hours` appear together
with `pct_of_limit`. Garmin tracks the limit either by distance or by time, and
most accounts set none at all - then those three fields are absent rather than
zero.

`include_retired` adds gear that was retired; off by default, because most
questions are about what is currently in rotation.

### `get_profile()`

`birth_date`, `gender`, `weight_kg`, `height_cm`, `vo2max_running`,
`vo2max_cycling`, `ftp_watt`, `lactate_threshold_hr`,
`lactate_threshold_speed_ms`, `critical_swim_speed_ms`, and `hr_zones` per sport
with the five zone floors.

Worth fetching before interpreting any training data.

### `whoami()`

`account` and `oauth2_expires_at`. Use it to tell "not connected" apart from
"no data".

---

## Errors you may see

| Message | What to do |
|---|---|
| `your Garmin account is not connected yet …` | Connect Garmin on the account page, or run `garmin-mcp login` locally |
| `no Garmin tokens found - run garmin-mcp login …` | Local install without a login |
| `activity <id> has no FIT original (imported from GPX/TCX?)` | The FIT tools need a file Garmin actually has; imported activities have none |
| `activity <id> not found` | Wrong id, or the activity was deleted |
| `Invalid isoformat string` | A date was not `YYYY-MM-DD` |

Errors arrive as readable text rather than a generic failure, so the model can
usually tell you what to fix.
