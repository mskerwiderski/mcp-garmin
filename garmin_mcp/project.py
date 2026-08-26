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
    return [compact({
        "uuid": g.get("uuid"),
        "name": g.get("name"),
        "type": g.get("gearType"),
        "brand": g.get("brand"),
        "status": g.get("status"),
        "used_km": _km(g.get("distanceUsedMeters")),
        "limit_km": _km(g.get("maxUsageDistanceMeters")),
        "first_use": g.get("firstUseDate"),
    }) for g in items]


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
