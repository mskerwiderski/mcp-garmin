# Vendored from MyFITContainer e9931f0: app/streams.py
# Source of truth stays MFC; changes here are marked "mcp-garmin addition".

"""Zeitreihen (record-Streams) aus einer FIT für die Detail-/Grafik-Ansicht.

Liest die record-Messages und liefert downgesampelte Arrays je Kanal plus die
GPS-Polyline + Bounding-Box. Reine Lese-Operation; das Original bleibt unberührt.
Fehlende Kanäle/defekte Dateien ergeben leere Resultate (keine Exception)."""

from __future__ import annotations

import io
import math
import warnings
from datetime import datetime, timedelta

import fitdecode

from .parser import _SPORT_NAMES, _enum_name

_SEMI = 180.0 / 2**31           # Semicircles -> Grad
_MAX_POINTS = 1500              # Ziel-Auflösung nach Downsampling

# Anzeige-Labels je Session-Sportart (Multisport-Beine)
_LEG_LABELS = {"swimming": "Swim", "cycling": "Bike", "running": "Run"}

# Bekannte Kanäle: key -> (FIT-Feldkandidaten in Priorität, Faktor). Werden
# kuratiert (Label/Farbe/Skalierung, Feldnamen-Aliasse je Gerät). ALLE übrigen
# numerischen record-Felder werden zusätzlich AUTOMATISCH als generische Kanäle
# erkannt (gerätunabhängig) — siehe _build_plan. Developer-Felder (Moxy/HRV/
# Core/Stryd) erscheinen in fitdecode unter ihrem Feldnamen wie native Felder.
_KNOWN = (
    ("hr",          ("heart_rate",), 1.0),
    ("power",       ("power",), 1.0),
    ("cadence",     ("cadence", "running_cadence"), 1.0),
    ("altitude",    ("enhanced_altitude", "altitude"), 1.0),
    ("speed",       ("enhanced_speed", "speed"), 3.6),     # m/s -> km/h
    ("distance",    ("distance",), 0.001),                 # m -> km (X-Achse)
    # erweiterte Streams; Feldnamen variieren je Sensor/Encoder (native vs.
    # Developer-Feld), daher mehrere Kandidaten.
    ("smo2",        ("SmO2", "saturated_hemoglobin_percent", "currHemoPerc"), 1.0),  # Muskel-O2 (%)
    ("hb_diff",     ("HbDiff", "total_hemoglobin_conc"), 1.0),       # Hb-Diff/tHb (g/dl)
    ("core_temp",   ("core_temperature",), 1.0),           # Körperkerntemp (°C)
    ("skin_temp",   ("SkinTemperature", "skin_temperature"), 1.0),   # Hauttemp (°C)
    ("temperature", ("temperature",), 1.0),                # Umgebung (°C)
    ("heat_strain", ("heat_strain_index",), 1.0),          # Hitzebelastung
    ("rmssd",       ("RMSSD",), 1.0),                       # HRV (ms)
    ("sdnn",        ("SDNN",), 1.0),                        # HRV (ms)
    ("dfa_a1",      ("Alpha1",), 1.0),                      # DFA alpha1
)

# Feldnamen, die schon von einem bekannten Kanal belegt sind:
_KNOWN_FIELDS = {c for _k, cands, _f in _KNOWN for c in cands}
# Numerisch, aber nicht als Kurve sinnvoll / anderweitig behandelt (GPS, Zeit,
# monoton akkumulierend, Enums):
_SKIP_FIELDS = {"timestamp", "position_lat", "position_long",
                "accumulated_power", "fractional_cadence", "activity_type"}

# Kuratiertes Label/Farbe je bekanntem Kanal-Key (für series_meta).
_META = {
    "hr": ("Heart rate (bpm)", "#ff5d57"),
    "power": ("Power (W)", "#f0a23a"),
    "cadence": ("Cadence", "#7b6cff"),
    "altitude": ("Altitude (m)", "#35c08a"),
    "speed": ("Speed (km/h)", "#4f8cff"),
    "pace": ("Pace (min/km)", "#4f8cff"),       # vom Detail-JS aus speed abgeleitet
    "smo2": ("SmO₂ (%)", "#e35d8a"),
    "hb_diff": ("Hb diff (g/dl)", "#c0566e"),
    "core_temp": ("Core temperature (°C)", "#ff8a3d"),
    "skin_temp": ("Skin temperature (°C)", "#f0a23a"),
    "temperature": ("Temperature (°C)", "#9aa7b6"),
    "heat_strain": ("Heat strain index", "#ff6b3d"),
    "rmssd": ("HRV RMSSD (ms)", "#7b6cff"),
    "sdnn": ("HRV SDNN (ms)", "#9b8cff"),
    "dfa_a1": ("DFA α1", "#35c0b0"),
}
# Farbpalette für automatisch erkannte (unbekannte) Kanäle.
_PALETTE = ("#e8896b", "#6bbf8a", "#8a7bff", "#d98ad0", "#5bb0c9",
            "#c9a35b", "#a0a8b4", "#d06b6b", "#6bb0d0", "#b0c95b")


def _humanize(name: str) -> str:
    s = name.replace("_", " ").strip()
    return s if (s[:1].isupper()) else (s[:1].upper() + s[1:])


def _build_plan(recs: list[dict]) -> list[tuple]:
    """Kanal-Plan für DIESE Datei: bekannte Kanäle (mit vorhandenen Kandidaten)
    + alle übrigen numerischen record-Felder als generische Kanäle. So werden
    Streams gerätunabhängig automatisch erkannt."""
    present: set[str] = set()
    for r in recs:
        for k, v in r.items():
            if isinstance(v, (int, float)):
                present.add(k)
    plan: list[tuple] = []
    for key, cands, factor in _KNOWN:
        here = tuple(c for c in cands if c in present)
        if here:
            plan.append((key, here, factor))
    for f in sorted(present - _KNOWN_FIELDS - _SKIP_FIELDS):
        plan.append((f, (f,), 1.0))                # generisch, Faktor 1.0
    return plan


def series_meta(keys) -> list[dict]:
    """[{key,label,color}] für die zu zeichnenden Kanal-Keys (Reihenfolge wie
    übergeben). Bekannte Keys kuratiert, unbekannte humanisiert + Paletten-Farbe."""
    out: list[dict] = []
    pi = 0
    for k in keys:
        if k in _META:
            label, color = _META[k]
        else:
            label, color = _humanize(k), _PALETTE[pi % len(_PALETTE)]
            pi += 1
        out.append({"key": k, "label": label, "color": color})
    return out


# Gruppen + Reihenfolge für die Linien-Auswahlboxen (Detail-Chart).
_CHART_GROUPS = [
    ("Heart & HRV", ["hr", "rmssd", "sdnn", "dfa_a1"]),
    ("Power & Pace", ["power", "pace", "speed", "cadence"]),
    ("Oxygen & Temperature", ["smo2", "hb_diff", "core_temp", "skin_temp",
                              "temperature", "heat_strain"]),
    ("Elevation", ["altitude"]),
]
# Running-dynamics: per Feldname erkannt (case-insensitiv, auto-detektierte Keys
# tragen den FIT-Feldnamen wie „Air Power"/„stance_time").
_RUNNING_DYNAMICS = {
    "air power", "form power", "effort pace", "leg spring stiffness",
    "impact loading rate", "stance time", "step length", "vertical oscillation",
    "vertical ratio", "muscle state", "muscle trend", "ground time",
    "ground contact time", "vertical_oscillation", "vertical_ratio",
    "stance_time", "step_length",
}
_KEY_GROUP = {k: title for title, keys in _CHART_GROUPS for k in keys}
_GROUP_ORDER = [t for t, _ in _CHART_GROUPS] + ["Running dynamics", "Other"]


def _chart_group(key: str) -> str:
    if key in _KEY_GROUP:
        return _KEY_GROUP[key]
    if key.replace("_", " ").lower() in _RUNNING_DYNAMICS:
        return "Running dynamics"
    return "Other"


# Schwimmen: Pace läuft pro 100 m (nicht min/km) und „Cadence" sind Züge/min.
_SWIM_LABELS = {"pace": "Pace (min/100m)", "cadence": "Strokes/min"}


def chart_options(keys, sport: str | None = None) -> list[dict]:
    """[{key,label,group}] der vorhandenen Plot-Kanäle, gruppiert + sortiert für
    die Linien-Auswahlboxen. Innerhalb bekannter Gruppen feste Reihenfolge,
    „Running dynamics"/„Other" alphabetisch nach Label. `sport` passt Labels an
    (Schwimmen: Pace /100m, Strokes/min)."""
    meta = {m["key"]: m["label"] for m in series_meta(keys)}
    if sport == "swimming":
        meta.update({k: v for k, v in _SWIM_LABELS.items() if k in meta})
    order_in_group = {k: i for _t, ks in _CHART_GROUPS for i, k in enumerate(ks)}
    items = [{"key": k, "label": meta[k], "group": _chart_group(k)} for k in keys]

    def sort_key(it):
        g = _GROUP_ORDER.index(it["group"])
        within = order_in_group.get(it["key"], 10_000)
        return (g, within, it["label"].lower())

    return sorted(items, key=sort_key)


def _coord(v):
    if not isinstance(v, (int, float)):
        return None
    d = v * _SEMI
    return d if -180.0 <= d <= 180.0 else None


def _ffill(seq: list) -> None:
    """Forward-Fill in place: None -> letzter bekannter Wert (führende None
    bleiben None). Für spärliche Slow-Sensor-Kanäle."""
    last = None
    for i, x in enumerate(seq):
        if x is not None:
            last = x
        elif last is not None:
            seq[i] = last


def _num(rec: dict, names, factor: float):
    for n in names:
        v = rec.get(n)
        if isinstance(v, (int, float)):
            return round(v * factor, 3)
    return None


def _read_records(data: bytes) -> tuple[list[dict], list[dict], list[dict]]:
    """Liest record-, session- UND lap-Messages in einem Durchlauf."""
    recs: list[dict] = []
    sess: list[dict] = []
    laps: list[dict] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with fitdecode.FitReader(io.BytesIO(data)) as fr:
            for frame in fr:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name == "record":
                    recs.append({f.name: f.value for f in frame.fields})
                elif frame.name == "session":
                    sess.append({f.name: f.value for f in frame.fields})
                elif frame.name == "lap":
                    laps.append({f.name: f.value for f in frame.fields})
    return recs, sess, laps


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Großkreis-Distanz zweier Punkte (Dezimalgrad) in Metern."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _gps_distance_from_records(recs: list[dict]) -> float | None:
    """Haversine-Summe über aufeinanderfolgende gültige GPS-Punkte (Meter).
    None bei <2 Positionen (kein verwertbarer GPS-Track)."""
    total = 0.0
    prev = None
    n = 0
    for r in recs:
        la, lo = _coord(r.get("position_lat")), _coord(r.get("position_long"))
        if la is None or lo is None or (la == 0 and lo == 0):
            continue
        if prev is not None:
            total += _haversine_m(prev[0], prev[1], la, lo)
        prev = (la, lo)
        n += 1
    return round(total, 1) if n >= 2 else None


def gps_track_distance(data: bytes) -> float | None:
    """Streckenlänge aus den GPS-Punkten der FIT (Meter); None ohne GPS-Track.
    Für Footpod-Runs weicht das von der aufgezeichneten total_distance ab."""
    recs, _, _ = _read_records(data)
    return _gps_distance_from_records(recs)


def _session_legs(sess: list[dict], t0) -> list[dict]:
    """Multisport-Beine als Auswahl-Metadaten: Label + Zeitfenster (Sekunden-
    Offsets passend zur t-Achse) + Kennzahlen aus der Session-Message.
    Wechselzonen heißen T1/T2…; doppelte Sportarten werden nummeriert
    (Duathlon: Run 1/Run 2). Nur bei >1 Session gefüllt."""
    if len(sess) < 2 or t0 is None:
        return []
    legs: list[dict] = []
    for d in sess:
        start = d.get("start_time")
        elapsed = d.get("total_elapsed_time")
        if not isinstance(elapsed, (int, float)):
            elapsed = d.get("total_timer_time")
        if not (isinstance(start, datetime) and isinstance(elapsed, (int, float))):
            continue
        sport = _enum_name(d.get("sport"), _SPORT_NAMES) or "generic"
        speed = d.get("enhanced_avg_speed")
        if not isinstance(speed, (int, float)):
            speed = d.get("avg_speed")
        legs.append({
            "sport": sport,
            "start_s": round((start - t0).total_seconds(), 1),
            "end_s": round((start - t0).total_seconds() + elapsed, 1),
            "dist_m": d.get("total_distance")
                      if isinstance(d.get("total_distance"), (int, float)) else None,
            "dur_s": round(d.get("total_timer_time") or elapsed),
            "avg_hr": d.get("avg_heart_rate")
                      if isinstance(d.get("avg_heart_rate"), (int, float)) else None,
            "avg_power": d.get("avg_power")
                         if isinstance(d.get("avg_power"), (int, float)) else None,
            "avg_speed_ms": speed if isinstance(speed, (int, float)) else None,
        })
    if len(legs) < 2:
        return []
    # Labels: Transitions als T1/T2…, Sport-Beine ggf. nummeriert
    base = [(_LEG_LABELS.get(l["sport"], l["sport"].capitalize())
             if l["sport"] != "transition" else "T") for l in legs]
    counts = {b: base.count(b) for b in base}
    seen: dict[str, int] = {}
    for leg, b in zip(legs, base):
        seen[b] = seen.get(b, 0) + 1
        if b == "T":
            leg["label"] = f"T{seen[b]}"
        else:
            leg["label"] = f"{b} {seen[b]}" if counts[b] > 1 else b
    return legs


def _laps(laps: list[dict], t0, names: dict | None = None) -> list[dict]:
    """Runden (lap-Messages) als Anzeige-/Zoom-Metadaten: Zeitfenster (Sekunden-
    Offsets passend zur t-Achse) + Kennzahlen direkt aus der lap-Message.
    `names` (Sidecar, {index-1-basiert-str: label}) überschreibt den Default-Namen.
    Leer, wenn <2 verwertbare Laps (eine Runde = die ganze Aktivität, uninteressant)."""
    if not laps or t0 is None:
        return []
    names = names or {}
    out: list[dict] = []
    for i, d in enumerate(laps):
        elapsed = d.get("total_elapsed_time")
        if not isinstance(elapsed, (int, float)):
            elapsed = d.get("total_timer_time")
        start = d.get("start_time")
        if not isinstance(start, datetime):
            end = d.get("timestamp")               # lap.timestamp = Endzeit
            if isinstance(end, datetime) and isinstance(elapsed, (int, float)):
                start = end - timedelta(seconds=elapsed)
        if not (isinstance(start, datetime) and isinstance(elapsed, (int, float))):
            continue
        # Erste Runde kann durch Rundungs-/Startversatz minimal vor t0 liegen -> auf 0.
        start_s = max(0.0, round((start - t0).total_seconds(), 1)) if i == 0 \
            else round((start - t0).total_seconds(), 1)
        speed = d.get("enhanced_avg_speed")
        if not isinstance(speed, (int, float)):
            speed = d.get("avg_speed")

        def _n(*keys):
            for k in keys:
                v = d.get(k)
                if isinstance(v, (int, float)):
                    return v
            return None

        dist_m = _n("total_distance")
        dur_s = round(d.get("total_timer_time") or elapsed)
        # avg_speed fehlt bei manchen Geräten in der lap-Message -> aus Distanz/Zeit
        # ableiten (Ø-Tempo der Runde, reicht für die Tabellen-Anzeige).
        if not isinstance(speed, (int, float)) and dist_m and dur_s:
            speed = dist_m / dur_s
        idx = i + 1
        out.append({
            "idx": idx,
            "name": names.get(str(idx)),
            "start_s": start_s,
            "end_s": round(start_s + elapsed, 1),
            "dist_m": dist_m,
            "dur_s": dur_s,
            "avg_hr": _n("avg_heart_rate"),
            "max_hr": _n("max_heart_rate"),
            "avg_power": _n("avg_power"),
            "max_power": _n("max_power"),
            "avg_cadence": _n("avg_cadence", "avg_running_cadence"),
            "avg_speed_ms": speed if isinstance(speed, (int, float)) else None,
        })
    return out if len(out) > 1 else []


_TRACK_MAX_POINTS = 400         # Heatmap-Auflösung je Aktivität (kompakt)


def extract_track(data: bytes) -> dict:
    """GPS-only, downsampled Route einer Aktivität für die Heatmap:
      {"track": [[lat,lng], …], "bounds": [[minlat,minlng],[maxlat,maxlng]]|None}.
    Leere/GPS-lose Datei -> {"track": [], "bounds": None}. Deutlich günstiger
    als extract_streams (keine Kanäle/Sessions, nur Positionen)."""
    out: dict = {"track": [], "bounds": None}
    try:
        recs, _, _ = _read_records(data)
    except Exception:  # noqa: BLE001 — defekte Datei -> leer
        return out
    pts: list[tuple[float, float]] = []
    for r in recs:
        la, lo = _coord(r.get("position_lat")), _coord(r.get("position_long"))
        if la is not None and lo is not None and not (la == 0 and lo == 0):
            pts.append((la, lo))
    if not pts:
        return out
    stride = max(1, -(-len(pts) // _TRACK_MAX_POINTS))    # ceil -> <= _TRACK_MAX_POINTS
    track = [[round(la, 5), round(lo, 5)]
             for i, (la, lo) in enumerate(pts) if i % stride == 0 or i == len(pts) - 1]
    lats = [p[0] for p in track]
    lngs = [p[1] for p in track]
    out["track"] = track
    out["bounds"] = [[min(lats), min(lngs)], [max(lats), max(lngs)]]
    return out


def _mean(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else None


def _rec_field(r: dict, *keys):
    for k in keys:
        v = r.get(k)
        if isinstance(v, (int, float)):
            return v
    return None


def _lap_window_stats(grp: list[dict], s0: float, s1: float) -> dict:
    """Anzeige-Kennzahlen einer Runde aus den Records im Fenster [s0, s1)."""
    hr = [r.get("heart_rate") for r in grp]
    pw = [r.get("power") for r in grp]
    cad = [_rec_field(r, "cadence", "running_cadence") for r in grp]
    spd = [_rec_field(r, "enhanced_speed", "speed") for r in grp]
    dists = [r.get("distance") for r in grp if isinstance(r.get("distance"), (int, float))]
    dist = (dists[-1] - dists[0]) if len(dists) >= 2 else None
    if dist is not None and dist < 0:
        dist = None
    dur = max(0.0, s1 - s0)
    avg_speed = (dist / dur) if (dist and dur > 0) else _mean(spd)
    hrv = [h for h in hr if isinstance(h, (int, float))]
    pwv = [p for p in pw if isinstance(p, (int, float))]
    mhr, mpw, mcad = _mean(hr), _mean(pw), _mean(cad)
    return {
        "dist_m": round(dist, 1) if dist is not None else None,
        "dur_s": round(dur),
        "avg_hr": round(mhr) if mhr is not None else None,
        "max_hr": max(hrv) if hrv else None,
        "avg_power": round(mpw) if mpw is not None else None,
        "max_power": max(pwv) if pwv else None,
        "avg_cadence": round(mcad) if mcad is not None else None,
        "avg_speed_ms": round(avg_speed, 3) if avg_speed is not None else None,
    }


def _laps_from_bounds(recs: list[dict], t0, bounds_s: list[float],
                      names: dict | None = None) -> list[dict]:
    """Runden aus editierten Grenzen `bounds_s` (aufsteigende Sekunden-Offsets inkl.
    0 und Endzeit): je Fenster die Kennzahlen aus den Records neu berechnen. Leer,
    wenn <2 Grenzen (= <1 Runde). Dieselbe Berechnung nutzt der Korrektur-FIT-Bau."""
    names = names or {}
    if t0 is None or not bounds_s or len(bounds_s) < 2:
        return []
    pts = []
    for r in recs:
        ts = r.get("timestamp")
        if isinstance(ts, datetime):
            pts.append((round((ts - t0).total_seconds(), 3), r))
    out = []
    n = len(bounds_s) - 1
    for i in range(n):
        s0, s1 = bounds_s[i], bounds_s[i + 1]
        last = (i == n - 1)
        grp = [r for (sec, r) in pts
               if s0 <= sec < s1 or (last and sec == s1)]
        st = _lap_window_stats(grp, s0, s1)
        st.update({"idx": i + 1, "name": names.get(str(i + 1)),
                   "start_s": round(s0, 1), "end_s": round(s1, 1)})
        out.append(st)
    return out


def lap_bounds_native(data: bytes) -> list[float]:
    """Native Lap-Grenzen der FIT als Sekunden-Offsets [0, …, Endzeit] — Startwert
    für den Editor / „auf Original zurück". Leer, wenn <2 Runden."""
    try:
        recs, _, laps = _read_records(data)
    except Exception:  # noqa: BLE001
        return []
    t0 = next((r["timestamp"] for r in recs if isinstance(r.get("timestamp"), datetime)), None)
    lp = _laps(laps, t0)
    if not lp:
        return []
    return [lp[0]["start_s"]] + [l["end_s"] for l in lp]


def extract_streams(data: bytes, lap_names: dict | None = None,
                    lap_bounds: list[float] | None = None) -> dict:
    """Liefert ein dict:
      n        – Anzahl gesampelter Punkte
      t        – x-Achse: vergangene Sekunden seit Start
      channels – {key: [werte|None]} nur für Kanäle mit echten Daten
                 (Kanäle, die nur None oder nur 0 enthalten, fehlen)
      gps      – [[lat,lng], …] (vereinfachte Route; ggf. leer)
      lat/lng  – sample-alignte Koordinaten (Länge == n, None ohne Fix);
                 für den Chart-Cursor -> Karten-Marker. Leer ohne GPS.
      sessions – Multisport-Beine ({label, sport, start_s, end_s, dist_m,
                 dur_s, avg_hr, avg_power, avg_speed_ms}); leer bei <2 Sessions
      bounds   – [[minlat,minlng],[maxlat,maxlng]] oder None
    """
    empty = {"n": 0, "t": [], "channels": {}, "gps": [], "lat": [], "lng": [],
             "maxima": {}, "sessions": [], "laps": [], "bounds": None,
             "te": None}
    try:
        recs, sess, laps = _read_records(data)
    except Exception:  # noqa: BLE001 — defekte Datei -> leeres Resultat
        return empty
    if not recs:
        return empty

    t0 = next((r["timestamp"] for r in recs if isinstance(r.get("timestamp"), datetime)), None)
    n = len(recs)
    stride = max(1, -(-n // _MAX_POINTS))    # ceil -> Ergebnis bleibt <= _MAX_POINTS

    plan = _build_plan(recs)                 # Kanäle dieser Datei automatisch erkennen
    t: list[float] = []
    cols: dict[str, list] = {key: [] for key, _, _ in plan}
    gps: list[list[float]] = []
    lat_arr: list = []
    lng_arr: list = []
    lat_min = lat_max = lng_min = lng_max = None

    for i, r in enumerate(recs):
        if i % stride != 0 and i != n - 1:
            continue
        ts = r.get("timestamp")
        t.append(round((ts - t0).total_seconds(), 1) if (isinstance(ts, datetime) and t0) else None)
        for key, names, factor in plan:
            cols[key].append(_num(r, names, factor))
        la, lo = _coord(r.get("position_lat")), _coord(r.get("position_long"))
        if la is not None and lo is not None and not (la == 0 and lo == 0):
            gps.append([round(la, 5), round(lo, 5)])
            lat_arr.append(round(la, 5))
            lng_arr.append(round(lo, 5))
            lat_min = la if lat_min is None else min(lat_min, la)
            lat_max = la if lat_max is None else max(lat_max, la)
            lng_min = lo if lng_min is None else min(lng_min, lo)
            lng_max = lo if lng_max is None else max(lng_max, lo)
        else:
            lat_arr.append(None)
            lng_arr.append(None)

    # Maxima über ALLE Records (nicht das downgesampelte cols) — sonst würde der
    # Spitzenwert (z. B. Max-Speed) vom Stride verschluckt.
    cmax: dict[str, float] = {}
    for r in recs:
        for key, names, factor in plan:
            v = _num(r, names, factor)
            if v is not None and (key not in cmax or v > cmax[key]):
                cmax[key] = v

    # nur Kanäle behalten, die echte Daten tragen (mind. ein Wert != None/0 —
    # ein durchgehend leerer oder 0-Stream ist kein anzeigbarer Datenstrom)
    channels = {k: v for k, v in cols.items()
                if any(x is not None and x != 0 for x in v)}
    # Spärliche Kanäle (langsame Sensoren wie Core-/Hauttemp, die nur alle paar
    # Sekunden senden) forward-fillen: sonst sind nach dem Downsampling fast nur
    # None übrig -> mit spanGaps:false keine sichtbare Linie. Dichte Kanäle mit
    # echten Aussetzern (HR/Power) bleiben unangetastet (Schwelle 90 %).
    for v in channels.values():
        nn = sum(1 for x in v if x is not None)
        if 0 < nn < 0.9 * len(v):
            _ffill(v)
    bounds = ([[lat_min, lng_min], [lat_max, lng_max]] if lat_min is not None else None)
    if not gps:
        lat_arr, lng_arr = [], []
    maxima = {k: cmax[k] for k in channels if k in cmax}
    # Editierte Grenzen (Sidecar) haben Vorrang -> Kennzahlen aus Records neu
    # berechnen; sonst die nativen Lap-Messages (Garmin-Werte).
    lap_list = (_laps_from_bounds(recs, t0, lap_bounds, lap_names) if lap_bounds
                else _laps(laps, t0, lap_names))
    return {"n": len(t), "t": t, "channels": channels, "gps": gps,
            "lat": lat_arr, "lng": lng_arr, "maxima": maxima,
            "sessions": _session_legs(sess, t0), "laps": lap_list,
            "bounds": bounds}


def extract_hr(data: bytes) -> dict:
    """Voll aufgelöste HR-Serie für den HR-Editor: EIN Eintrag je Record-Message
    (gnum 20), in Dateireihenfolge — der Index deckt sich exakt mit dem Byte-Writer
    in hr_edit.apply_hr_edits (KEIN Downsampling, anders als extract_streams).
      t   – Sekunden seit Start (oder None)
      hr  – bpm oder None (kein/ungültiger Wert)
    {n, t, hr, has_hr, t0_epoch} — t0_epoch = UTC-Epoch des ersten Records
    (t + t0_epoch = absolute Record-Zeit, z. B. fürs CSV-Mapping)."""
    empty = {"n": 0, "t": [], "hr": [], "has_hr": False, "t0_epoch": None}
    try:
        recs, _, _ = _read_records(data)
    except Exception:        # noqa: BLE001 — defekte Datei -> leeres Resultat
        return empty
    if not recs:
        return empty
    t0 = next((r["timestamp"] for r in recs
               if isinstance(r.get("timestamp"), datetime)), None)
    t: list = []
    hr: list = []
    for r in recs:
        ts = r.get("timestamp")
        t.append(round((ts - t0).total_seconds(), 1)
                 if (isinstance(ts, datetime) and t0) else None)
        v = r.get("heart_rate")
        hr.append(int(v) if isinstance(v, (int, float)) and v > 0 else None)
    return {"n": len(recs), "t": t, "hr": hr,
            "has_hr": any(x is not None for x in hr),
            "t0_epoch": t0.timestamp() if t0 else None}


def _secs(d: dict):
    v = d.get("total_timer_time")
    if not isinstance(v, (int, float)):
        v = d.get("total_elapsed_time")
    return round(v, 1) if isinstance(v, (int, float)) else None


def extract_swim(data: bytes) -> dict:
    """Bahn-Schwimmen für Tempo-Grafik + Intervalltabelle:
      pool_m  – Beckenlänge (m)
      lengths – je Bahn: {active, stroke, sec, pace100 (s/100m, nur aktiv), hr}
      laps    – aktive Intervalle: {dist, lengths, sec, pace100, stroke, hr, spl}
                (hr = Ø Herzfrequenz, spl = Ø Züge je Bahn)
    """
    out = {"pool_m": None, "lengths": [], "laps": []}
    windows: list = []          # (start, end) je Bahn, für die per-Bahn-HR
    recs: list = []             # (timestamp, heart_rate)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fitdecode.FitReader(io.BytesIO(data)) as fr:
                for frame in fr:
                    if not isinstance(frame, fitdecode.FitDataMessage):
                        continue
                    if frame.name == "session":
                        v = {f.name: f.value for f in frame.fields}.get("pool_length")
                        if isinstance(v, (int, float)) and v > 0:
                            out["pool_m"] = float(v)
                    elif frame.name == "record":
                        d = {f.name: f.value for f in frame.fields}
                        ts, hr = d.get("timestamp"), d.get("heart_rate")
                        if isinstance(ts, datetime) and isinstance(hr, (int, float)):
                            recs.append((ts, int(hr)))
                    elif frame.name == "length":
                        d = {f.name: f.value for f in frame.fields}
                        stroke = d.get("swim_stroke")
                        out["lengths"].append({
                            "active": str(d.get("length_type") or "active") == "active",
                            "stroke": str(stroke) if stroke else None,
                            "sec": _secs(d),
                        })
                        start = d.get("start_time")
                        elapsed = d.get("total_elapsed_time")
                        if not isinstance(elapsed, (int, float)):
                            elapsed = d.get("total_timer_time")
                        windows.append((start, start + timedelta(seconds=elapsed))
                                       if isinstance(start, datetime)
                                       and isinstance(elapsed, (int, float)) else None)
                    elif frame.name == "lap":
                        d = {f.name: f.value for f in frame.fields}
                        dist, sec = d.get("total_distance"), _secs(d)
                        if isinstance(dist, (int, float)) and dist > 0 and sec:
                            st = d.get("swim_stroke")
                            n = int(d.get("num_active_lengths") or 0)
                            strokes = d.get("total_strokes")        # swim-Alias …
                            if not isinstance(strokes, (int, float)):
                                strokes = d.get("total_cycles")     # … bzw. Basisfeld
                            hr = d.get("avg_heart_rate")
                            out["laps"].append({
                                "dist": round(dist),
                                "lengths": n,
                                "sec": sec,
                                "pace100": round(sec * 100.0 / dist, 1),
                                "stroke": str(st) if st else None,
                                "hr": int(hr) if isinstance(hr, (int, float)) else None,
                                "spl": (round(strokes / n)
                                        if isinstance(strokes, (int, float)) and n else None),
                            })
    except Exception:  # noqa: BLE001
        return {"pool_m": None, "lengths": [], "laps": []}

    pool = out["pool_m"]
    for L, w in zip(out["lengths"], windows):
        L["pace100"] = (round(L["sec"] * 100.0 / pool, 1)
                        if (L["active"] and pool and L["sec"]) else None)
        L["hr"] = None
        if w and recs:
            vals = [h for (t, h) in recs if w[0] <= t <= w[1]]
            if vals:
                L["hr"] = round(sum(vals) / len(vals))
    return out


def _swim_lap_assignment(laps: list[dict], lengths_raw: list[dict],
                         n: int) -> list[int]:
    """0-basierter Intervall-Index je Bahn (für den Längen-Editor): bevorzugt die
    Index-Bereiche der Lap-Messages (first_length_index/num_lengths), sonst die
    Zeitfenster der Lap-Starts, sonst alles in Lap 0. Best-effort — dient nur der
    Gruppierung/den Trennlinien, nicht der Korrektheit der Totals."""
    lap_of = [0] * n
    if n == 0:
        return lap_of
    ranges, ok = [], True
    for lp in laps:
        fli, nl = lp.get("first_length_index"), lp.get("num_lengths")
        if isinstance(fli, int) and isinstance(nl, int) and nl > 0:
            ranges.append((fli, nl))
        else:
            ok = False
            break
    if ok and ranges:
        for li, (fli, nl) in enumerate(ranges):
            for idx in range(fli, min(fli + nl, n)):
                if 0 <= idx < n:
                    lap_of[idx] = li
        return lap_of
    starts = sorted(s for lp in laps
                    if isinstance((s := lp.get("start_time")), datetime))
    if starts and all(isinstance(lengths_raw[i].get("start_time"), datetime)
                      for i in range(n)):
        for i in range(n):
            ls = lengths_raw[i]["start_time"]
            li = 0
            for k, s in enumerate(starts):
                if s <= ls:
                    li = k
                else:
                    break
            lap_of[i] = li
    return lap_of


def extract_swim_edit(data: bytes) -> dict:
    """Editierbare Bahnen-Rohdaten für den Längen-Editor (anders als extract_swim,
    das Anzeige-Aggregate liefert):
      pool_m  – Beckenlänge (m)
      lengths – je Bahn: {active, stroke (kleingeschrieben|None), timer_s,
                elapsed_s, strokes (nur aktiv), hr, lap (0-basierter Intervall-Index)}
    Quelle ist immer das Original; die gespeicherten Edits liegen in der Sidecar."""
    from . import fit_split
    out: dict = {"pool_m": None, "lengths": []}
    try:
        m = fit_split._read(data)
    except Exception:        # noqa: BLE001 — Editor ist best-effort
        return out
    sessions = m.get("sessions") or []
    if sessions:
        pl = sessions[0].get("pool_length")
        if isinstance(pl, (int, float)) and pl > 0:
            out["pool_m"] = float(pl)
    raw = m.get("lengths") or []
    recs = [(t, int(h)) for d in (m.get("records") or [])
            if isinstance((t := d.get("timestamp")), datetime)
            and isinstance((h := d.get("heart_rate")), (int, float))]
    n = len(raw)
    lap_of = _swim_lap_assignment(m.get("laps") or [], raw, n)
    for i, d in enumerate(raw):
        active = str(d.get("length_type") or "active") == "active"
        stroke = d.get("swim_stroke")
        timer = d.get("total_timer_time")
        elapsed = d.get("total_elapsed_time")
        if not isinstance(timer, (int, float)):
            timer = elapsed
        if not isinstance(elapsed, (int, float)):
            elapsed = timer
        strokes = d.get("total_strokes")
        if not isinstance(strokes, (int, float)):
            strokes = d.get("total_cycles")
        hr = None
        start, el = d.get("start_time"), elapsed
        if isinstance(start, datetime) and isinstance(el, (int, float)) and recs:
            end = start + timedelta(seconds=el)
            vals = [h for (t, h) in recs if start <= t <= end]
            if vals:
                hr = round(sum(vals) / len(vals))
        out["lengths"].append({
            "active": active,
            "stroke": str(stroke).lower() if stroke else None,
            "timer_s": round(float(timer), 2) if isinstance(timer, (int, float)) else 0.0,
            "elapsed_s": round(float(elapsed), 2) if isinstance(elapsed, (int, float)) else 0.0,
            "strokes": (int(strokes) if isinstance(strokes, (int, float)) and active
                        else None),
            "hr": hr,
            "lap": lap_of[i],
            # Original-Index (0-basiert, Stream-Reihenfolge) als Herkunfts-Anker für
            # den verlustfreien Rebuild: der Editor führt ihn als `src` mit, swim_rewrite
            # kopiert darüber das Original-Payload (alle Felder) bzw. aggregiert es.
            "i": i,
        })
    return out


# ---------- Disk-Cache für die Detailseite ----------

def extract_streams_cached(
    store_dir: str,
    sha: str,
    data: bytes,
    lap_names: dict | None = None,
    lap_bounds: list[float] | None = None,
    want_swim: bool = False,
) -> tuple[dict, dict]:
    """extract_streams (+ optional extract_swim) mit Disk-Cache.

    Der volle fitdecode-Parse kostet bei langen Einheiten Sekunden — pro
    Aktivität wird deshalb genau EIN gzip-JSON unter
    <store_dir>/cache/streams/<sha>.json.gz gehalten. Der Key hasht die
    zu parsenden Bytes (Korrektur-FIT ODER Original -> jede Korrektur
    invalidiert automatisch) plus die Lap-Edits; bei Mismatch wird neu
    geparst und überschrieben (kein unbegrenztes Wachstum).
    Cache-Fehler jeder Art fallen lautlos auf den frischen Parse zurück.
    """
    import gzip
    import hashlib
    import json as _json
    import os

    _FMT = "v3"  # bei Format-Änderungen an extract_streams/extract_swim bumpen
    key_src = _json.dumps([_FMT, want_swim, lap_names, lap_bounds],
                          sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(hashlib.sha256(data).digest()
                         + key_src.encode("utf-8")).hexdigest()[:24]

    cache_dir = os.path.join(store_dir, "cache", "streams")
    path = os.path.join(cache_dir, f"{sha}.json.gz")
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            cached = _json.load(fh)
        if cached.get("key") == key:
            return cached["st"], cached["swim"]
    except Exception:  # noqa: BLE001 — fehlt/korrupt/altes Format -> neu parsen
        pass

    st = extract_streams(data, lap_names=lap_names, lap_bounds=lap_bounds)
    swim = extract_swim(data) if want_swim else {"pool_m": None, "lengths": [],
                                                 "laps": []}
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            _json.dump({"key": key, "st": st, "swim": swim}, fh,
                       ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — Cache ist optional (z. B. RO-Dateisystem)
        pass
    return st, swim


