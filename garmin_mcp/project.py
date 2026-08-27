"""Projections: turn Garmin's very wide JSON into something an LLM can read.

Garmin answers with hundreds of fields per activity and an activity detail can
run into megabytes. Every tool result goes through here first - a whitelist per
shape, rounded numbers, no nulls. This is not cosmetic: handing the raw payload
to the model burns the context window that the actual conversation needs.
"""

from __future__ import annotations


def _round(v, digits=2):
    return round(v, digits) if isinstance(v, (int, float)) else None


def _km(m):
    return round(m / 1000.0, 3) if isinstance(m, (int, float)) else None


def _min(s):
    return round(s / 60.0, 1) if isinstance(s, (int, float)) else None


def compact(d: dict) -> dict:
    """Drop keys whose value is None or an empty list/dict."""
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


def activity_summary(a: dict) -> dict:
    """One row of the activity list."""
    t = a.get("activityType") or {}
    return compact({
        "activity_id": a.get("activityId"),
        "name": a.get("activityName"),
        "start_local": a.get("startTimeLocal"),
        "sport": t.get("typeKey"),
        "distance_km": _km(a.get("distance")),
        "duration_min": _min(a.get("duration")),
        "moving_min": _min(a.get("movingDuration")),
        "avg_hr": _round(a.get("averageHR"), 0),
        "max_hr": _round(a.get("maxHR"), 0),
        "avg_power": _round(a.get("avgPower"), 0),
        "avg_speed_ms": _round(a.get("averageSpeed")),
        "elevation_gain_m": _round(a.get("elevationGain"), 0),
        "calories": _round(a.get("calories"), 0),
    })


def activity_detail(d: dict) -> dict:
    """Garmin's own view of one activity (no FIT download involved)."""
    s = d.get("summaryDTO") or {}
    t = d.get("activityTypeDTO") or {}
    meta = d.get("metadataDTO") or {}
    return compact({
        "activity_id": d.get("activityId"),
        "name": (d.get("activityName") or "").strip() or None,
        "description": (d.get("description") or "").strip() or None,
        "sport": t.get("typeKey"),
        "event_type": (d.get("eventTypeDTO") or {}).get("typeKey"),
        "start_local": s.get("startTimeLocal"),
        "start_gmt": s.get("startTimeGMT"),
        "timezone": (d.get("timeZoneUnitDTO") or {}).get("timeZone"),
        "distance_km": _km(s.get("distance")),
        "duration_min": _min(s.get("duration")),
        "moving_min": _min(s.get("movingDuration")),
        "elapsed_min": _min(s.get("elapsedDuration")),
        "avg_hr": _round(s.get("averageHR"), 0),
        "max_hr": _round(s.get("maxHR"), 0),
        "avg_power": _round(s.get("averagePower"), 0),
        "max_power": _round(s.get("maxPower"), 0),
        "normalized_power": _round(s.get("normPower"), 0),
        "avg_speed_ms": _round(s.get("averageSpeed")),
        "max_speed_ms": _round(s.get("maxSpeed")),
        "avg_cadence": _round(s.get("averageRunningCadenceInStepsPerMinute")
                              or s.get("averageBikingCadenceInRevPerMinute"), 0),
        "elevation_gain_m": _round(s.get("elevationGain"), 0),
        "elevation_loss_m": _round(s.get("elevationLoss"), 0),
        "calories": _round(s.get("calories"), 0),
        "training_effect_aerobic": _round(s.get("trainingEffect"), 1),
        "training_effect_anaerobic": _round(s.get("anaerobicTrainingEffect"), 1),
        "avg_temperature_c": _round(s.get("averageTemperature"), 1),
        "water_temp_c": _round(s.get("waterTemperature"), 1),
        "elevation_corrected": meta.get("elevationCorrected"),
        "gear": [g.get("displayName") for g in (d.get("gear") or []) if g.get("displayName")],
    })


def sleep_summary(dto: dict, scores: dict | None = None) -> dict:
    sc = (scores or {}).get("overall") or {}
    return compact({
        "sleep_h": _round((dto.get("sleepTimeSeconds") or 0) / 3600, 2) or None,
        "deep_min": _min(dto.get("deepSleepSeconds")),
        "light_min": _min(dto.get("lightSleepSeconds")),
        "rem_min": _min(dto.get("remSleepSeconds")),
        "awake_min": _min(dto.get("awakeSleepSeconds")),
        "avg_respiration": _round(dto.get("averageRespirationValue"), 1),
        "avg_spo2": _round(dto.get("averageSpO2Value"), 1),
        "score": sc.get("value"),
        "score_label": sc.get("qualifierKey"),
    })


def daily_summary(d: dict) -> dict:
    return compact({
        "steps": d.get("totalSteps"),
        "step_goal": d.get("dailyStepGoal"),
        "distance_km": _km(d.get("totalDistanceMeters")),
        "calories_total": d.get("totalKilocalories"),
        "calories_active": d.get("activeKilocalories"),
        "resting_hr": d.get("restingHeartRate"),
        "min_hr": d.get("minHeartRate"),
        "max_hr": d.get("maxHeartRate"),
        "stress_avg": d.get("averageStressLevel"),
        "stress_max": d.get("maxStressLevel"),
        "body_battery_high": d.get("bodyBatteryHighestValue"),
        "body_battery_low": d.get("bodyBatteryLowestValue"),
        "body_battery_charged": d.get("bodyBatteryChargedValue"),
        "body_battery_drained": d.get("bodyBatteryDrainedValue"),
        "respiration_avg": d.get("avgWakingRespirationValue"),
        "intensity_minutes": d.get("moderateIntensityMinutes"),
        "vigorous_minutes": d.get("vigorousIntensityMinutes"),
        "floors_climbed": d.get("floorsAscended"),
    })


def training_readiness(items: list[dict]) -> dict:
    """The morning entry (inputContext AFTER_WAKEUP_RESET) is Garmin's own
    Morning Report value; its absence means the watch has not synced yet."""
    if not items:
        return {}
    best = next((i for i in items if i.get("inputContext") == "AFTER_WAKEUP_RESET"),
                items[0])
    return compact({
        "score": best.get("score"),
        "level": best.get("level"),
        "feedback": best.get("feedbackShort"),
        "sleep_score": best.get("sleepScore"),
        "recovery_time_h": _round((best.get("recoveryTime") or 0) / 60, 1) or None,
        "hrv_factor": best.get("hrvFactorPercent"),
        "stress_history_factor": best.get("stressHistoryFactorPercent"),
        "acute_load": best.get("acuteLoad"),
        "is_morning_value": best.get("inputContext") == "AFTER_WAKEUP_RESET",
    })


def training_status(d: dict) -> dict:
    latest = (d.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
    entry = next(iter(latest.values()), {}) if isinstance(latest, dict) else {}
    load = (d.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
    lentry = next(iter(load.values()), {}) if isinstance(load, dict) else {}
    vo2 = (d.get("mostRecentVO2Max") or {})
    return compact({
        "status": entry.get("trainingStatusFeedbackPhrase"),
        "acute_load": (entry.get("acuteTrainingLoadDTO") or {}).get("acwrPercent"),
        "load_ratio": (entry.get("acuteTrainingLoadDTO") or {}).get("acwrStatus"),
        "load_focus": lentry.get("trainingBalanceFeedbackPhrase"),
        "load_low_aerobic": lentry.get("monthlyLoadAerobicLow"),
        "load_high_aerobic": lentry.get("monthlyLoadAerobicHigh"),
        "load_anaerobic": lentry.get("monthlyLoadAnaerobic"),
        "vo2max_running": ((vo2.get("generic") or {}).get("vo2MaxPreciseValue")),
        "vo2max_cycling": ((vo2.get("cycling") or {}).get("vo2MaxPreciseValue")),
    })


def gear(items: list[dict]) -> list[dict]:
    out = []
    for g in items:
        used_m = g.get("distanceUsedMeters")
        limit_m = g.get("maxUsageDistanceMeters")
        limit_s = g.get("maxUsageDurationSeconds")
        row = {
            "uuid": g.get("uuid"),
            "name": g.get("name"),
            "type": g.get("gearType"),
            "brand": g.get("brand"),
            "status": g.get("status"),
            "activities": g.get("numActivitiesLinked"),
            "used_km": _km(used_m),
            "used_hours": _round((g.get("durationUsedSeconds") or 0) / 3600, 1) or None,
            "days_used": g.get("daysUsed"),
            "first_use": g.get("firstUseDate"),
            # Garmin tracks a replacement limit by distance or by time; both are
            # 0 or absent when nobody set one.
            "limit_km": _km(limit_m) or None,
            "limit_hours": _round((limit_s or 0) / 3600, 1) or None,
        }
        if limit_m and used_m:
            row["pct_of_limit"] = round(used_m / limit_m * 100, 1)
        elif limit_s and g.get("durationUsedSeconds"):
            row["pct_of_limit"] = round(g["durationUsedSeconds"] / limit_s * 100, 1)
        out.append(compact(row))
    return out


def profile(settings: dict, bio: dict, zones: list[dict]) -> dict:
    css = bio.get("criticalSwimSpeed")          # mm/s
    return compact({
        "birth_date": settings.get("birthDate"),
        "gender": settings.get("gender"),
        "weight_kg": _round((settings.get("weight") or 0) / 1000, 1) or None,
        "height_cm": _round(settings.get("height"), 0),
        "vo2max_running": bio.get("vo2Max"),
        "vo2max_cycling": bio.get("vo2MaxCycling"),
        "ftp_watt": bio.get("functionalThresholdPower"),
        "lactate_threshold_hr": bio.get("lactateThresholdHeartRate"),
        "lactate_threshold_speed_ms": _round(bio.get("lactateThresholdSpeed")),
        "critical_swim_speed_ms": _round(css / 1000, 3) if isinstance(css, (int, float)) else None,
        "hr_zones": [compact({
            "sport": z.get("sport"),
            "max_hr": z.get("maxHeartRateUsed"),
            "resting_hr": z.get("restingHeartRateUsed"),
            "floors": [z.get(f"zone{i}Floor") for i in range(1, 6)],
        }) for z in zones],
    })


# Garmin's socialChallengeActivityTypeId. All three count metres.
CHALLENGE_SPORT = {1: "running", 2: "cycling", 3: "swimming"}


def _challenge_state(start: str, end: str) -> str:
    """From the dates, not from socialChallengeStatusId - the numeric status is
    undocumented, the dates are not."""
    from datetime import date
    today = date.today().isoformat()
    if start and today < start:
        return "upcoming"
    return "running" if end and today <= end else "finished"


def challenge_summary(c: dict) -> dict:
    start, end = (c.get("startDate") or "")[:10], (c.get("endDate") or "")[:10]
    return compact({
        "challenge_id": c.get("uuid"),
        "name": c.get("adHocChallengeName"),
        "sport": CHALLENGE_SPORT.get(c.get("socialChallengeActivityTypeId")),
        "state": _challenge_state(start, end),
        "start": start,
        "end": end,
        "my_rank": c.get("userRanking"),
        # A running challenge reports 0 here; the real count comes with the
        # leaderboard from get_challenge.
        "players": c.get("playerCount") or None,
    })


def challenge_detail(c: dict, my_display_name: str = "") -> dict:
    sport = CHALLENGE_SPORT.get(c.get("socialChallengeActivityTypeId"))
    players = c.get("players") or []
    board = []
    for p in sorted(players, key=lambda p: p.get("ranking") or 999):
        total = p.get("totalNumber")
        row = {"rank": p.get("ranking"), "name": p.get("fullName"),
               "total_km": _km(total) if sport else None,
               "last_sync": (p.get("lastSyncTime") or "")[:10]}
        if my_display_name and p.get("displayName") == my_display_name:
            row["is_you"] = True
        board.append(compact(row))
    return compact({**challenge_summary(c), "players": len(players) or None,
                    "leaderboard": board})


# --- fitness metrics -------------------------------------------------------

# Garmin identifies personal records by a numeric type only. These labels were
# derived from the values themselves (a 5407 next to a half marathon is a time,
# a 42999 next to a long run is metres), not from a documented list - unknown
# ids are passed through unlabelled rather than guessed at.
PR_TYPES = {
    1: ("1 km", "time_s"),
    2: ("1 mile", "time_s"),
    3: ("5 km", "time_s"),
    4: ("10 km", "time_s"),
    5: ("half marathon", "time_s"),
    6: ("marathon", "time_s"),
    7: ("longest run", "distance_m"),
    8: ("longest ride", "distance_m"),
    9: ("biggest climb", "elevation_m"),
}


def _hms(seconds) -> str | None:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def personal_records(items: list[dict]) -> list[dict]:
    out = []
    for r in items:
        label, kind = PR_TYPES.get(r.get("typeId"), (None, None))
        value = r.get("value")
        row = {
            "record": label,
            "type_id": r.get("typeId"),
            "sport": r.get("activityType"),
            "date": (r.get("actStartDateTimeInGMTFormatted") or "")[:10],
            "activity_id": r.get("activityId"),
            "activity_name": r.get("activityName"),
        }
        if kind == "time_s":
            row["time"] = _hms(value)
            row["time_s"] = _round(value, 1)
        elif kind == "distance_m":
            row["distance_km"] = _km(value)
        elif kind == "elevation_m":
            row["elevation_m"] = _round(value, 0)
        else:
            row["value"] = _round(value, 2)
        out.append(compact(row))
    return sorted(out, key=lambda r: (r.get("type_id") or 0))


def fitness_metrics(predictions: dict, age: dict, endurance: dict, hill: dict,
                    totals: dict) -> dict:
    races = {}
    for key, label in (("time5K", "5k"), ("time10K", "10k"),
                       ("timeHalfMarathon", "half_marathon"),
                       ("timeMarathon", "marathon")):
        races[label] = _hms(predictions.get(key))
    metrics = (totals.get("userMetrics") or [{}])[0]
    return compact({
        "race_predictions": compact(races),
        "fitness_age": _round(age.get("fitnessAge"), 1),
        "chronological_age": age.get("chronologicalAge"),
        "achievable_fitness_age": _round(age.get("achievableFitnessAge"), 1),
        "endurance_score": endurance.get("overallScore"),
        "hill_score": hill.get("overallScore"),
        "hill_strength": hill.get("strengthScore"),
        "hill_endurance": hill.get("enduranceScore"),
        "vo2max": hill.get("vo2MaxPreciseValue"),
        "lifetime": compact({
            "activities": metrics.get("totalActivities"),
            "distance_km": _km(metrics.get("totalDistance")),
            "hours": _round((metrics.get("totalDuration") or 0) / 3600, 0) or None,
            "elevation_m": _round(metrics.get("totalElevationGain"), 0),
        }),
    })


# --- trends ----------------------------------------------------------------


def health_trend(steps: list[dict], battery: list[dict],
                 vo2max: list[dict]) -> list[dict]:
    """One row per day, merged from three range endpoints. The Body Battery
    response also carries the full intraday curve - dropped here, since a month
    of curves is tens of thousands of numbers."""
    by_day: dict[str, dict] = {}
    for s in steps:
        day = s.get("calendarDate")
        if day:
            by_day.setdefault(day, {"day": day}).update({
                "steps": s.get("totalSteps"),
                "step_goal": s.get("stepGoal"),
                "distance_km": _km(s.get("totalDistance")),
            })
    for b in battery:
        day = b.get("date")
        if day:
            by_day.setdefault(day, {"day": day}).update({
                "body_battery_charged": b.get("charged"),
                "body_battery_drained": b.get("drained"),
            })
    for v in vo2max:
        run, bike = v.get("generic") or {}, v.get("cycling") or {}
        day = run.get("calendarDate") or bike.get("calendarDate")
        if day:
            by_day.setdefault(day, {"day": day}).update(compact({
                "vo2max_running": run.get("vo2MaxPreciseValue"),
                "vo2max_cycling": bike.get("vo2MaxPreciseValue"),
            }))
    return [compact(by_day[d]) for d in sorted(by_day)]


# --- plan and races --------------------------------------------------------


def planned_workout(w: dict) -> dict:
    sport = (w.get("sportType") or {}).get("sportTypeKey")
    return compact({
        "workout_id": w.get("workoutId"),
        "name": w.get("workoutName"),
        "sport": sport,
        "description": (w.get("description") or "").strip() or None,
        # Garmin stores 0 for "not estimated"; reporting a 0 minute workout
        # would be worse than saying nothing.
        "estimated_duration_min": _min(w.get("estimatedDurationInSecs")) or None,
        "estimated_distance_km": _km(w.get("estimatedDistanceInMeters")) or None,
        "updated": (w.get("updateDate") or "")[:10],
    })


# Calendar entries this connector reports. Activities, weight and blood
# pressure entries also live in the calendar but are covered by their own
# tools, so they are filtered out rather than duplicated.
CALENDAR_TYPES = ("workout", "event")


def calendar_item(i: dict) -> dict:
    """One scheduled item. Accounts differ in what they schedule - a training
    plan fills this with workouts, a racer with events - so every field is
    optional and missing ones simply do not appear."""
    target = i.get("completionTarget") or {}
    sport = (i.get("sportTypeKey") or (i.get("sportType") or {}).get("sportTypeKey")
             or i.get("workoutSportTypeKey"))
    return compact({
        "type": i.get("itemType"),
        "id": i.get("workoutId") or i.get("id"),
        "title": i.get("title") or i.get("workoutName"),
        "date": i.get("date"),
        "sport": sport,
        "start_time": (i.get("eventTimeLocal") or {}).get("startTimeHhMm"),
        "timezone": (i.get("eventTimeLocal") or {}).get("timeZoneId"),
        "is_race": i.get("isRace"),
        "training_plan_id": i.get("trainingPlanId"),
        "planned_duration_min": _min(i.get("duration") or i.get("estimatedDurationInSecs")),
        "planned_distance_km": _km(i.get("distance") or i.get("estimatedDistanceInMeters")),
        "target": (f"{target.get('value')} {target.get('unit')}"
                   if target.get("value") else None),
        "completed": i.get("completed"),
    })


# --- per-activity context --------------------------------------------------


def time_in_zones(zones: list[dict], unit: str = "bpm") -> list[dict]:
    """Only zones with time in them; a list of five zeroes tells nobody
    anything."""
    return [compact({
        "zone": z.get("zoneNumber"),
        f"from_{unit}": z.get("zoneLowBoundary"),
        "minutes": _min(z.get("secsInZone")),
    }) for z in zones if (z.get("secsInZone") or 0) > 0]


def weather(w: dict) -> dict:
    """Garmin serves this endpoint in Fahrenheit and mph whatever the account
    settings say - everything else in this connector is metric, so convert."""
    def c_from_f(f):
        return round((f - 32) * 5 / 9, 1) if isinstance(f, (int, float)) else None

    def kmh(mph):
        return round(mph * 1.609344, 1) if isinstance(mph, (int, float)) else None

    return compact({
        "temperature_c": c_from_f(w.get("temp")),
        "feels_like_c": c_from_f(w.get("apparentTemp")),
        "dew_point_c": c_from_f(w.get("dewPoint")),
        "humidity_pct": w.get("relativeHumidity"),
        "wind_kmh": kmh(w.get("windSpeed")),
        "wind_gust_kmh": kmh(w.get("windGust")),
        "wind_from": w.get("windDirectionCompassPoint"),
        "conditions": (w.get("weatherTypeDTO") or {}).get("desc"),
    })
