# Vendored from MyFITContainer e9931f0: app/fit_parser.py
# Source of truth stays MFC; changes here are marked "mcp-garmin addition".

"""FIT-Datei -> Kennzahlen. Nutzt fitdecode (robust gegen die fehlerhaften
intervals.icu-Felder, die fitparse hart ablehnen würde).

Alle Kennzahlen kommen aus der ersten `session`-Message; fehlt ein Feld,
bleibt es None. Quer über die Quellen (Garmin, Apple HealthKit, Coros, Zwift,
FORM, WorkOutDoors, intervals.icu) sind die session-Feldnamen konsistent;
die Varianten (running_cadence vs. cadence, enhanced_* Geschwindigkeit,
fehlendes total_ascent) werden hier normalisiert.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import asdict, dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime
from typing import Optional

import fitdecode


class FitParseError(Exception):
    """Datei ist nicht als FIT lesbar."""


@dataclass
class FitMetrics:
    sport: Optional[str] = None
    sub_sport: Optional[str] = None
    start_time: Optional[datetime] = None
    duration_s: Optional[int] = None       # total_moving_time (bewegt / netto, = Strava)
    elapsed_s: Optional[int] = None        # total_elapsed_time (gesamt / brutto)
    distance_m: Optional[float] = None
    calories: Optional[int] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    avg_power: Optional[int] = None
    max_power: Optional[int] = None
    norm_power: Optional[int] = None
    avg_speed_ms: Optional[float] = None
    total_ascent_m: Optional[int] = None
    total_descent_m: Optional[int] = None
    avg_cadence: Optional[int] = None
    tss: Optional[float] = None
    device: Optional[str] = None
    serial_number: Optional[int] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None        # Modell/Produkt
    software_version: Optional[str] = None
    # Produzierende CIQ-App aus der developer_data_id-UUID (z. B. „cellTrainer
    # 1.2.3 (45)"). Zuverlässiger Fingerprint der eigenen App, auch wenn sie sich
    # via file_id als Fake-Garmin-Device (FR310XT) ausgibt. None bei Fremdquellen.
    produced_by: Optional[str] = None
    title: Optional[str] = None          # workout.wkt_name
    description: Optional[str] = None     # session.description
    start_lat: Optional[float] = None    # Grad (aus Semicircles)
    start_lng: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


# Kanonische Liste der abgeleiteten Kennzahl-Felder (für Re-Parse/Reset).
METRIC_FIELDS: tuple[str, ...] = tuple(f.name for f in dataclass_fields(FitMetrics))


def _as_int(v) -> Optional[int]:
    return int(v) if isinstance(v, (int, float)) else None


def _as_float(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


def _as_str(v) -> Optional[str]:
    if v is None:
        return None
    return str(v)


# Garmin sport/sub_sport-Namen — Fallback für Werte, die fitdecodes (älteres)
# Profil numerisch zurückgibt (z. B. brick=80).
_SPORT_NAMES = {
    0: "generic", 1: "running", 2: "cycling", 3: "transition",
    4: "fitness_equipment", 5: "swimming", 8: "tennis", 10: "training",
    11: "walking", 17: "hiking", 18: "multisport", 64: "racket",
}
_SUB_SPORT_NAMES = {
    0: "generic", 17: "lap_swimming", 18: "open_water",
    19: "flexibility_training", 20: "strength_training", 22: "match",
    26: "cardio_training", 32: "bike_to_run_transition",
    33: "run_to_bike_transition", 34: "swim_to_bike_transition",
    78: "triathlon", 79: "duathlon", 80: "brick", 81: "swim_run",
    95: "badminton", 97: "table_tennis",
}


def _enum_name(v, table: dict) -> Optional[str]:
    """Belässt von fitdecode aufgelöste Namen; rein numerische Werte werden auf
    den Garmin-Namen gemappt (sonst als Zahl-String beibehalten)."""
    if v is None:
        return None
    if isinstance(v, str) and not v.isdigit():
        return v
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return str(v)
    return table.get(iv, str(iv))


def _clean_text(v) -> Optional[str]:
    """Freitext säubern: geschützte Leerzeichen normalisieren, trimmen,
    leere Strings -> None."""
    if v is None:
        return None
    t = str(v).replace("\xa0", " ").strip()
    return t or None


def _power_fallback(session_avg, session_max, rp: dict):
    """Liefert (avg_power, max_power). Bevorzugt die Session-Werte; fehlen sie,
    werden sie aus dem record.power-Aggregat (rp) berechnet — viele Lauf-Power-
    Quellen (Coros/Stryd) füllen nur die records, nicht die session-Summe."""
    avg = session_avg
    if avg is None and rp["count"]:
        avg = round(rp["sum"] / rp["count"])
    mx = session_max
    if mx is None and rp["max"] is not None:
        mx = int(rp["max"])
    return avg, mx


def _coord(v, limit: float) -> Optional[float]:
    """FIT-Semicircles -> Dezimalgrad; ungültige/leere Werte -> None."""
    if not isinstance(v, (int, float)):
        return None
    deg = v * (180.0 / 2**31)
    return deg if -limit <= deg <= limit else None


_RECORD_GPS_SCAN_LIMIT = 120  # so viele records nach dem ersten GPS-Fix absuchen


def _first_messages(data: bytes) -> dict:
    """Sammelt die für das Mapping relevanten Messages. Wirft FitParseError,
    wenn die Datei gar nicht gelesen werden kann."""
    out = {"file_id": {}, "session": {}, "sessions": [], "workout": {},
           "first_gps": {}, "file_creator": {}, "device_info": {},
           # NUR der device_info mit device_index=='creator' (= Aufzeichnungsgerät);
           # KEIN Fallback auf den ersten (das wäre oft ein Sensor: hrm_pro/gnss).
           "device_creator": {},
           # developer_data_id-Messages (je CIQ-App application_id-UUID + -version)
           # -> Produzenten-Erkennung (cellTrainer-Fingerprint).
           "dev_apps": [],
           # Aggregat aus record.power — Fallback, wenn die session keine
           # Power-Summe trägt (z. B. Coros/Stryd Running Power).
           "record_power": {"sum": 0.0, "count": 0, "max": None},
           # Summe der aktiven Bahnen-Zeit (length-Messages) — das ist die echte
           # Schwimm-Bewegungszeit (= Strava "Moving"), auch wenn die session kein
           # total_moving_time trägt (gilt für ältere Pool-Swims).
           "swim_active_s": {"sum": 0.0, "n": 0}}
    rec_scanned = 0
    seen_device_info = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fitdecode.FitReader(io.BytesIO(data)) as fr:
                for frame in fr:
                    if not isinstance(frame, fitdecode.FitDataMessage):
                        continue
                    n = frame.name
                    if n == "file_id" and not out["file_id"]:
                        out["file_id"] = {f.name: f.value for f in frame.fields}
                    elif n == "session":
                        sd = {f.name: f.value for f in frame.fields}
                        out["sessions"].append(sd)
                        if not out["session"]:
                            out["session"] = sd
                    elif n == "length":
                        d = {f.name: f.value for f in frame.fields}
                        if str(d.get("length_type") or "active") == "active":
                            sec = d.get("total_timer_time")
                            if not isinstance(sec, (int, float)):
                                sec = d.get("total_elapsed_time")
                            if isinstance(sec, (int, float)):
                                out["swim_active_s"]["sum"] += sec
                                out["swim_active_s"]["n"] += 1
                    elif n == "workout" and not out["workout"]:
                        out["workout"] = {f.name: f.value for f in frame.fields}
                    elif n == "file_creator" and not out["file_creator"]:
                        out["file_creator"] = {f.name: f.value for f in frame.fields}
                    elif n == "developer_data_id":
                        out["dev_apps"].append({f.name: f.value for f in frame.fields})
                    elif n == "device_info":
                        d = {f.name: f.value for f in frame.fields}
                        # bevorzugt das "creator"-Geraet (= Aufzeichnungsgeraet)
                        if d.get("device_index") == "creator":
                            out["device_info"] = d
                            out["device_creator"] = d
                        elif not seen_device_info:
                            out["device_info"] = d
                            seen_device_info = True
                    elif n == "record":
                        # Ein Durchlauf über die Felder: Power (immer
                        # akkumulieren) + erster GPS-Fix (nur im Scan-Fenster).
                        need_gps = (
                            not out["first_gps"]
                            and rec_scanned < _RECORD_GPS_SCAN_LIMIT
                        )
                        pwr = lat = lng = None
                        for fld in frame.fields:
                            if fld.name == "power":
                                pwr = fld.value
                            elif need_gps and fld.name == "position_lat":
                                lat = fld.value
                            elif need_gps and fld.name == "position_long":
                                lng = fld.value
                        if isinstance(pwr, (int, float)):
                            rp = out["record_power"]
                            rp["sum"] += pwr
                            rp["count"] += 1
                            if rp["max"] is None or pwr > rp["max"]:
                                rp["max"] = pwr
                        if need_gps:
                            rec_scanned += 1
                            if lat is not None and lng is not None:
                                out["first_gps"] = {"lat": lat, "long": lng}
    except Exception as exc:  # fitdecode wirft diverse Fehlertypen
        raise FitParseError(str(exc)) from exc
    return out


def _pick_product(d: dict):
    """Modell-Feld aus einer Message (file_id/device_info). Garmin nennt es
    `garmin_product` (von fitdecode zu Namen aufgelöst, z. B. fenix8/edge_820)."""
    return (_clean_text(d.get("product_name")) or _clean_text(d.get("garmin_product"))
            or _clean_text(d.get("product")))


def _model_product(file_id: dict, dev: dict):
    """Modell des AUFNEHMENDEN Geräts. device_info(creator) hat Vorrang vor file_id,
    sofern Hersteller übereinstimmt — frühe Garmin-Firmware (z. B. Fenix 3, 2015/16)
    schrieb fälschlich fr920xt (1765) ins file_id, während device_info korrekt das
    Gerät (fenix3) meldet."""
    fid_m = _clean_text(file_id.get("manufacturer"))
    dev_m = _clean_text((dev or {}).get("manufacturer"))
    if dev and dev_m and dev_m == fid_m:
        return _pick_product(dev) or _pick_product(file_id)
    return _pick_product(file_id) or (_pick_product(dev) if not fid_m else None)


def _device_str(manufacturer, product) -> Optional[str]:
    parts = [str(p) for p in (manufacturer, product) if p not in (None, "", 0)]
    return " / ".join(parts) or None


def _session_dur(se: dict):
    d = se.get("total_moving_time")
    if not isinstance(d, (int, float)):
        d = se.get("total_timer_time")
    if not isinstance(d, (int, float)):
        d = se.get("total_elapsed_time")
    return d if isinstance(d, (int, float)) else None


def _session_elapsed(se: dict):
    d = se.get("total_elapsed_time")
    if not isinstance(d, (int, float)):
        d = se.get("total_timer_time")
    return d if isinstance(d, (int, float)) else None


def _session_speed(se: dict):
    v = se.get("enhanced_avg_speed")
    if not isinstance(v, (int, float)):
        v = se.get("avg_speed")
    return v if isinstance(v, (int, float)) else None


def _session_cadence(se: dict):
    v = se.get("avg_running_cadence")
    if not isinstance(v, (int, float)):
        v = se.get("avg_cadence")
    return v if isinstance(v, (int, float)) else None


def _multisport_sub_sport(distinct: set[str]) -> Optional[str]:
    """Disziplin-Label aus den beteiligten Sportarten ableiten."""
    if {"swimming", "cycling", "running"} <= distinct:
        return "triathlon"
    if distinct == {"cycling", "running"}:
        return "duathlon"
    if distinct == {"swimming", "running"}:
        return "aquathlon"
    return None


def _aggregate_multisport(sessions: list[dict], distinct: set[str]) -> dict:
    """Fasst die einzelnen Beine (inkl. Wechselzonen) eines Multisport-Events
    zu Gesamt-Kennzahlen zusammen: Summen für additive Groessen, gewichtete
    Mittel (nach Bein-Dauer) für Durchschnitte, Maxima für die Spitzenwerte."""

    def _vals(getter):
        return [g for se in sessions if (g := getter(se)) is not None]

    def _sum(getter):
        vals = _vals(getter)
        return sum(vals) if vals else None

    def _max(getter):
        vals = _vals(getter)
        return max(vals) if vals else None

    def _wavg(getter):
        num = den = 0.0
        for se in sessions:
            v, w = getter(se), _session_dur(se)
            if isinstance(v, (int, float)) and isinstance(w, (int, float)) and w > 0:
                num += v * w
                den += w
        return (num / den) if den else None

    def _num(field):
        return lambda se: se.get(field) if isinstance(se.get(field), (int, float)) else None

    def _total_tss():
        """TSS nur, wenn JEDES Nicht-Wechsel-Bein einen mitbringt — sonst None.

        Garmin schreibt training_stress_score je Bein und oft nur für einzelne:
        der Koppel-Duathlon vom 2026-08-08 hat 19 Sessions (10 Beine + 9
        Wechsel) und TSS an genau EINEM Rad-Bein (26.4). Die Summe wäre dann
        nicht die Gesamtbelastung, sondern der Wert eines einzelnen Beins — und
        weil sie > 0 ist, hielte fitness.effective_tss sie für einen echten
        Wert und schätzte nicht mehr aus der Herzfrequenz. Ein 4:15-h-Duathlon
        ginge so mit 26 TSS in die Formkurve. Ohne Wert schätzt MFC den ganzen
        Multisport als hrTSS — unvollständig summieren ist schlechter als gar
        nicht summieren."""
        legs = [se for se in sessions
                if _enum_name(se.get("sport"), _SPORT_NAMES) != "transition"]
        vals = [se.get("training_stress_score") for se in legs]
        if not legs or not all(isinstance(v, (int, float)) for v in vals):
            return None
        return sum(vals)

    starts = [se.get("start_time") for se in sessions
              if isinstance(se.get("start_time"), datetime)]
    descs = [_clean_text(se.get("description")) for se in sessions]

    _dur, _elapsed = _sum(_session_dur), _sum(_session_elapsed)
    if (isinstance(_dur, (int, float)) and isinstance(_elapsed, (int, float))
            and _dur > _elapsed):  # Moving nie > Gesamtzeit
        _dur = _elapsed
    return {
        "sport": "multisport",
        "sub_sport": _multisport_sub_sport(distinct),
        "start_time": min(starts) if starts else None,
        "duration_s": _as_int(_dur),
        "elapsed_s": _as_int(_elapsed),
        "distance_m": _as_float(_sum(_num("total_distance"))),
        "calories": _as_int(_sum(_num("total_calories"))),
        "avg_hr": _as_int(_wavg(_num("avg_heart_rate"))),
        "max_hr": _as_int(_max(_num("max_heart_rate"))),
        "avg_power": _as_int(_wavg(_num("avg_power"))),
        "max_power": _as_int(_max(_num("max_power"))),
        "norm_power": _as_int(_wavg(_num("normalized_power"))),
        "avg_speed_ms": _as_float(_wavg(_session_speed)),
        "total_ascent_m": _as_int(_sum(_num("total_ascent"))),
        "total_descent_m": _as_int(_sum(_num("total_descent"))),
        "avg_cadence": _as_int(_wavg(_session_cadence)),
        "tss": _as_float(_total_tss()),
        "description": next((d for d in descs if d), None),
    }


def parse_sessions(data: bytes) -> list[dict]:
    """Die rohen `session`-Messages (ein Eintrag je Bein, Wechsel inbegriffen).

    Basis für die TSS-Schätzung: die braucht das einzelne Bein UND den
    Athleten (FTP, Schwellentempo, CSS) — Athletenwissen gehört nicht in den
    Parser, deshalb liefert er nur die Rohdaten (siehe tss_estimate)."""
    return _first_messages(data)["sessions"]


def parse_fit(data: bytes) -> FitMetrics:
    """Extrahiert Kennzahlen aus FIT-Bytes. Wirft FitParseError nur, wenn die
    Datei nicht als FIT lesbar ist; fehlende Einzelfelder ergeben None.

    Multisport (mehrere `session`-Messages mit verschiedenen Sportarten, z. B.
    Triathlon) wird erkannt und ueber alle Beine aggregiert (sport=multisport)
    statt nur das erste Bein zu nehmen."""
    m = _first_messages(data)
    file_id, s, workout, first_gps = m["file_id"], m["session"], m["workout"], m["first_gps"]
    dev, fc = m["device_info"], m["file_creator"]
    if not file_id and not s:
        # Ein gültiges FIT hat mindestens eine file_id-Message; fehlt selbst
        # die (z. B. leere Datei), ist es keine verwertbare FIT-Datei.
        raise FitParseError("no FIT messages found")

    _manufacturer = _clean_text(file_id.get("manufacturer"))
    _product = _model_product(file_id, m["device_creator"])
    from . import devfields as fit_devfields  # mcp-garmin: module renamed
    _produced_by = fit_devfields.producer_from_dev_ids(m["dev_apps"])
    _sw = dev.get("software_version") or fc.get("software_version")
    _serial = _as_int(file_id.get("serial_number")) or _as_int(dev.get("serial_number"))

    start = s.get("start_time") or file_id.get("time_created")
    # Moving (netto, = Strava "Moving Time"):
    #  - Pool-Schwimmen: Summe der aktiven Bahnen — zieht die Bahnen-Pausen ab.
    #    Der Timer tut das NICHT (timer≈elapsed), und total_moving_time fehlt in
    #    älteren FITs; die Bahnen-Summe rekonstruiert die echte Bewegungszeit.
    #  - sonst: total_moving_time (>0, wenn vorhanden), dann timer, dann elapsed.
    duration = None
    _sa = m["swim_active_s"]
    if _sa["n"] > 0:
        duration = _sa["sum"]
    if duration is None:
        _mv = s.get("total_moving_time")
        if isinstance(_mv, (int, float)) and _mv > 0:
            duration = _mv
    if duration is None:
        duration = s.get("total_timer_time")
    if duration is None:
        duration = s.get("total_elapsed_time")
    elapsed = s.get("total_elapsed_time")
    if elapsed is None:
        elapsed = s.get("total_timer_time")
    # Moving kann nie länger als die Gesamtzeit sein — bei manchen Trainer-/
    # Virtual-Aktivitäten liegt total_moving_time durch FIT-Rundung ein paar
    # Sekunden über total_elapsed_time; auf elapsed deckeln.
    if (isinstance(duration, (int, float)) and isinstance(elapsed, (int, float))
            and duration > elapsed):
        duration = elapsed
    speed = s.get("enhanced_avg_speed")
    if speed is None:
        speed = s.get("avg_speed")
    cadence = s.get("avg_running_cadence")
    if cadence is None:
        cadence = s.get("avg_cadence")
    _start_lat = _coord(s.get("start_position_lat"), 90)
    _start_lng = _coord(s.get("start_position_long"), 180)
    if (_start_lat is None or _start_lng is None) and first_gps:
        # session ohne start_position -> ersten GPS-Record nehmen
        _start_lat = _coord(first_gps.get("lat"), 90)
        _start_lng = _coord(first_gps.get("long"), 180)
    if _start_lat == 0 and _start_lng == 0:  # 0/0 ist kein realer Startpunkt
        _start_lat = _start_lng = None

    avg_power, max_power = _power_fallback(
        _as_int(s.get("avg_power")), _as_int(s.get("max_power")), m["record_power"]
    )

    metrics = FitMetrics(
        sport=_enum_name(s.get("sport"), _SPORT_NAMES),
        sub_sport=_enum_name(s.get("sub_sport"), _SUB_SPORT_NAMES),
        start_time=start if isinstance(start, datetime) else None,
        duration_s=_as_int(duration),
        elapsed_s=_as_int(elapsed),
        distance_m=_as_float(s.get("total_distance")),
        calories=_as_int(s.get("total_calories")),
        avg_hr=_as_int(s.get("avg_heart_rate")),
        max_hr=_as_int(s.get("max_heart_rate")),
        avg_power=avg_power,
        max_power=max_power,
        norm_power=_as_int(s.get("normalized_power")),
        avg_speed_ms=_as_float(speed),
        total_ascent_m=_as_int(s.get("total_ascent")),
        total_descent_m=_as_int(s.get("total_descent")),
        avg_cadence=_as_int(cadence),
        tss=_as_float(s.get("training_stress_score")),
        device=_device_str(_manufacturer, _product),
        serial_number=_serial,
        manufacturer=_manufacturer,
        product=_product,
        produced_by=_produced_by,
        software_version=_clean_text(_sw),
        title=_clean_text(workout.get("wkt_name")),
        description=_clean_text(s.get("description")),
        start_lat=_start_lat,
        start_lng=_start_lng,
    )

    # Multisport: mehrere Beine mit unterschiedlichen Sportarten -> aggregieren.
    sessions = m["sessions"]
    distinct = {
        sp for se in sessions
        if (sp := _enum_name(se.get("sport"), _SPORT_NAMES))
        and sp != "transition"
    }
    is_multisport = (
        str(file_id.get("type")) == "multisport" or len(distinct) > 1
    )
    if is_multisport and sessions:
        for field, value in _aggregate_multisport(sessions, distinct).items():
            setattr(metrics, field, value)

    return metrics
