"""The MCP tools. Read-only: nothing here writes to Garmin Connect.

Docstrings are the tool descriptions the model sees, so they say when to reach
for which tool - especially where Garmin's own numbers (get_activity) and the
device's file (analyze_activity_fit) can disagree.
"""

from __future__ import annotations

import functools
from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import fitview, project
from .client import GarminError
from .session import GarminSession, NotConnected

MAX_LIMIT = 100


def _guard(fn):
    """Turn our own failures into ToolError, which the SDK passes through to
    the client. Without this the model only ever sees "Error executing tool
    <name>" - and the most common failure here (no tokens yet) is exactly the
    one the user needs to read."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (NotConnected, GarminError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
    return wrapper


def _day(value: str | None) -> str:
    """Validate a YYYY-MM-DD day, defaulting to today."""
    if not value:
        return date.today().isoformat()
    return date.fromisoformat(value).isoformat()


async def _fit(session: GarminSession, activity_id: int) -> bytes:
    data = await session.fit_bytes(activity_id)
    if not data:
        raise ValueError(
            f"activity {activity_id} has no FIT original (imported from GPX/TCX?)")
    return data


def register(server: MCPServer, session: GarminSession) -> MCPServer:
    # ---------------------------------------------------------- activities

    @server.tool()
    @_guard
    async def list_activities(date_from: str | None = None,
                              date_to: str | None = None,
                              sport: str | None = None,
                              limit: int = 20) -> list[dict]:
        """List activities from Garmin Connect, newest first.

        date_from/date_to are local calendar days ("YYYY-MM-DD"). sport is a
        Garmin type key such as "running", "cycling", "lap_swimming",
        "open_water_swimming", "strength_training", "multi_sport". limit is
        capped at 100. Returns one compact row per activity; use get_activity
        for the full record and analyze_activity_fit for what the device
        actually recorded."""
        c = await session.client()
        rows = await c.search_activities(
            limit=max(1, min(int(limit), MAX_LIMIT)),
            start_date=date_from, end_date=date_to, activity_type=sport)
        return [project.activity_summary(a) for a in rows]

    @server.tool()
    @_guard
    async def get_activity(activity_id: int) -> dict:
        """Garmin Connect's own record of one activity: name, description,
        sport, times, distance, heart rate, power, elevation, training effect
        and assigned gear.

        These are the values Garmin computed server-side. Where elevation or
        pause handling is in question, compare with analyze_activity_fit,
        which reads the file the watch wrote."""
        c = await session.client()
        return project.activity_detail(await c.get_activity_detail(int(activity_id)))

    # ---------------------------------------------------------- FIT analysis

    @server.tool()
    @_guard
    async def analyze_activity_fit(activity_id: int) -> dict:
        """Download the original FIT of an activity and report the metrics as
        the device recorded them: sport/sub-sport, moving and elapsed time,
        distance, average and max heart rate, power, cadence, elevation,
        device model, and for multisport files the individual legs.

        Use this when Garmin's own numbers look wrong or when the question is
        about what the watch actually measured. The file is cached, so a
        follow-up get_activity_streams for the same activity is cheap."""
        return fitview.metrics(await _fit(session, int(activity_id)))

    @server.tool()
    @_guard
    async def get_activity_streams(activity_id: int,
                                   channels: list[str] | None = None,
                                   max_points: int = 120,
                                   include_gps: bool = False) -> dict:
        """Time series of an activity from its FIT file: heart rate, power,
        speed, cadence, altitude, temperature and any Connect-IQ channel the
        file carries (Stryd, SmO2, CORE temperature).

        The series is resampled to max_points (default 120, maximum 500) and
        every channel reports min/max/avg, so the result stays readable.
        Call it once without `channels` to see available_channels, then again
        with the ones you need. include_gps adds the route polyline."""
        data = await _fit(session, int(activity_id))
        return fitview.stream_view(data, channels=channels,
                                   max_points=max_points, include_gps=include_gps)

    @server.tool()
    @_guard
    async def get_swim_detail(activity_id: int) -> dict:
        """Pool swim detail from the FIT: pool length, every single length
        with stroke, time and pace per 100 m, plus the active intervals with
        stroke count. Only meaningful for lap_swimming activities."""
        return fitview.swim_view(await _fit(session, int(activity_id)))

    @server.tool()
    @_guard
    async def get_activity_sensors(activity_id: int) -> list[dict]:
        """Which Connect-IQ / developer fields the FIT of an activity carries,
        grouped by the app that wrote them, with a sample value and a flag for
        fields that stayed empty or constant - that is how you tell whether an
        external sensor (Stryd, Moxy/SmO2, CORE) was actually connected."""
        return fitview.sensors(await _fit(session, int(activity_id)))

    # ---------------------------------------------------------- health

    @server.tool()
    @_guard
    async def get_daily_health(day: str | None = None) -> dict:
        """Everything Garmin knows about one day: steps, calories, resting
        heart rate, stress, Body Battery, respiration, intensity minutes, plus
        the night's sleep (phases, score), HRV status and training readiness.

        day is "YYYY-MM-DD" and defaults to today. Sleep is filed under the
        morning you woke up."""
        d = _day(day)
        c = await session.client()
        display = await session.display_id()
        summary = await c.get_daily_summary(display, d)
        sleep_full = await c.get_sleep_full(display, d)
        dto = (sleep_full or {}).get("dailySleepDTO") or {}
        hrv = await c.get_hrv_summary(d)
        readiness = await c.get_training_readiness(d)
        return project.compact({
            "day": d,
            **project.daily_summary(summary),
            "sleep": project.sleep_summary(dto, dto.get("sleepScores")),
            "hrv_last_night": hrv.get("lastNightAvg"),
            "hrv_status": hrv.get("status"),
            "hrv_baseline_low": (hrv.get("baseline") or {}).get("lowUpper"),
            "hrv_baseline_high": (hrv.get("baseline") or {}).get("balancedUpper"),
            "training_readiness": project.training_readiness(readiness),
        })

    @server.tool()
    @_guard
    async def get_training_status(day: str | None = None) -> dict:
        """Garmin's aggregated training status for a day: the status phrase
        (PRODUCTIVE, MAINTAINING, OVERREACHING …), acute load and load ratio,
        the load focus split (low aerobic / high aerobic / anaerobic) and
        VO2max for running and cycling."""
        c = await session.client()
        return project.training_status(await c.get_training_status(_day(day)))

    @server.tool()
    @_guard
    async def get_body_composition(date_from: str, date_to: str) -> list[dict]:
        """Weight and body composition per day in a date range
        ("YYYY-MM-DD"). Weight and muscle mass come back in kilograms."""
        c = await session.client()
        rows = await c.get_body_composition(_day(date_from), _day(date_to))
        return [project.compact({
            "day": r.get("calendarDate"),
            "weight_kg": round(r["weight"] / 1000, 2) if r.get("weight") else None,
            "body_fat_pct": r.get("bodyFat"),
            "muscle_mass_kg": (round(r["muscleMass"] / 1000, 2)
                               if r.get("muscleMass") else None),
            "bmi": r.get("bmi"),
            "source": r.get("sourceType"),
        }) for r in rows]

    @server.tool()
    @_guard
    async def get_blood_pressure(date_from: str, date_to: str) -> list[dict]:
        """Blood pressure measurements in a date range ("YYYY-MM-DD"):
        systolic, diastolic, pulse and the measurement timestamp."""
        c = await session.client()
        raw = await c.get_blood_pressure(_day(date_from), _day(date_to))
        out = []
        for summary in (raw.get("measurementSummaries") or []):
            for m in (summary.get("measurements") or []):
                out.append(project.compact({
                    "measured_at": m.get("measurementTimestampLocal"),
                    "systolic": m.get("systolic"),
                    "diastolic": m.get("diastolic"),
                    "pulse": m.get("pulse"),
                    "note": m.get("notes"),
                }))
        return out

    # ---------------------------------------------------------- profile

    @server.tool()
    @_guard
    async def list_gear() -> list[dict]:
        """Active gear (shoes, bikes, …) with type, brand, accumulated
        distance and the distance limit set for it. Retired gear is not
        listed."""
        c = await session.client()
        return project.gear(await c.list_gear())

    @server.tool()
    @_guard
    async def get_profile() -> dict:
        """The athlete profile behind the numbers: birth date, gender, weight,
        height, VO2max for running and cycling, FTP, lactate threshold heart
        rate and speed, critical swim speed, and the heart rate zones per
        sport. Useful before interpreting any training data."""
        c = await session.client()
        return project.profile(await c.user_settings(),
                               await c.personal_information(),
                               await c.heart_rate_zones())

    @server.tool()
    @_guard
    async def whoami() -> dict:
        """Which Garmin account this server is connected to, and how long the
        current access token is still valid. Use it to check the connection
        before blaming empty results on missing data."""
        from .session import probe
        return await probe(session)

    return server
