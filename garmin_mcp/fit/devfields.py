# Vendored from MyFITContainer e9931f0: app/fit_devfields.py (read-only part).
# Only list_dev_fields() and its helpers; the byte-level strip() writer is not
# part of this read-only connector.

"""Connect-IQ-/Developer-Felder einer FIT auflisten und entfernen.

Garmin-Aktivitäten tragen CIQ-Datenfelder (Stryd, SmO2/CurrHemoPct, CORE Temp …)
als **Developer-Felder**:
  - `developer_data_id` (global msg 207): je CIQ-App `application_id` (UUID) +
    `developer_data_index`.
  - `field_description` (global msg 206): je Feld `developer_data_index`,
    `field_definition_number`, base-type, `field_name`, `units`.
Die Felder hängen als „developer fields" an Definition-Messages (Record-Header-Bit
`0x20` → dev-field-count + je Feld {field_number, size, developer_data_index}) und
tragen Bytes in den Data-Messages.

`list_dev_fields()` listet sie gruppiert je App (inkl. Sample + „leer/konstant"-
Heuristik, um Felder ohne getragenen Sensor zu erkennen). `strip()` schneidet
gewählte Einzelfelder ODER ganze Sensoren byte-genau heraus: betroffene
Definition-Messages (dev-Defs raus, Count/Flag anpassen) + Data-Messages (Bytes
raus) werden umgeschrieben, `field_description`/`developer_data_id` der entfernten
App entfallen, header `data_size` + CRC (+ chained Chunks) werden neu gesetzt;
alles Übrige bleibt byte-genau. So verschwinden die leeren/falschen CIQ-Statistiken
auf Garmin (nach Re-Upload über den Replace-Flow).
"""
from __future__ import annotations

import io
import logging
import warnings

import fitdecode
from fitdecode import types as _ft


log = logging.getLogger("garmin_mcp.devfields")

FIELD_DESCRIPTION = 206
DEVELOPER_DATA_ID = 207
_HEUR_MAX = 1500          # Sample-Cap je Feld für die „leer/konstant"-Heuristik

# Feste application_id der App „cellTrainer" (in der developer_data_id-Message).
_CELLTRAINER_UUID = "2a563aec-721a-4790-87ed-1fa8adbf7348"

# Bekannte Sensoren aus den CIQ-Feldnamen (lowercase-Teilstrings -> Anzeigename).
# Abgeglichen wird gegen normalisierte Feldnamen (lower + „_" -> Leerzeichen), damit
# sowohl „core temperature" als auch „core_temperature" matchen. Abgekürzte
# Schreibweisen (Train.Red: currHemoPerc/O2Hb/HHb) sind zusätzlich als Tokens drin.
_SENSOR_HINTS = [
    ({"leg spring stiffness", "form power", "air power"}, "Stryd"),
    ({"core temperature", "core body temperature", "skin temperature",
      "heat strain index"}, "CORE body temp"),
    ({"smo2", "saturated hemoglobin", "current hemoglobin", "currhemopct",
      "total hemoglobin", "thb", "hemoglobin", "hemoperc", "hemoconc",
      "o2hb"}, "Muscle O₂ (SmO2)"),
    ({"alpha1", "dfa a1", "rra1"}, "HRV (DFA a1)"),
]

# Bekannte CIQ-Apps über die application_id (erste 8 Hex-Zeichen); greift erst
# nach den Feldnamen-Hints. FORM-Datenfeld (Freiwasser mit Goggles-Kopplung):
# ca3ce193-fffc-4fae-9740-8a2d4c41d196, Felder form_device_serial_number,
# is_form_device_connected, location_accuracy, speed_from_15s_distance …
_UUID_NAMES = {
    "ca3ce193": "FORM Smart Swim Goggles",
}


def _uuid(b) -> str | None:
    # fitdecode liefert application_id je nach Version als bytes ODER als
    # Tuple/Liste von 16 Ints — beides akzeptieren.
    if isinstance(b, (tuple, list)):
        try:
            b = bytes(int(x) & 0xFF for x in b)
        except (TypeError, ValueError):
            return None
    if isinstance(b, (bytes, bytearray)) and len(b) == 16:
        h = bytes(b).hex()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    return None


def _produced_by(uuid: str | None, app_version_raw) -> str | None:
    """„cellTrainer {major}.{minor}.{patch} ({build})", wenn `uuid` die cellTrainer-
    application_id ist (case-insensitiv); cellTrainer packt Version+Build als
    M_mm_pp_bbb in das uint32 application_version. Ohne/0 Version nur „cellTrainer".
    None, wenn die UUID nicht matcht."""
    if not uuid or uuid.lower() != _CELLTRAINER_UUID:
        return None
    v = app_version_raw if isinstance(app_version_raw, int) else 0
    if v <= 0:
        return "cellTrainer"
    major = v // 10_000_000
    minor = (v // 100_000) % 100
    patch = (v // 1_000) % 100
    build = v % 1_000
    return f"cellTrainer {major}.{minor}.{patch} ({build})"


def producer_from_dev_ids(dev_apps: list[dict]) -> str | None:
    """Aus den developer_data_id-Messages (als Dicts {field_name: value}) den
    Produzenten ableiten, sofern eine bekannte App (cellTrainer) dabei ist —
    z. B. „cellTrainer 1.2.3 (45)". None, wenn keine erkannt. Erlaubt der
    Aktivitäts-Übersicht, cellTrainer auch dann als Quelle auszuweisen, wenn die
    file_id ein Fake-Garmin-Device (FR310XT) trägt."""
    for d in dev_apps or []:
        pb = _produced_by(_uuid(d.get("application_id")), d.get("application_version"))
        if pb:
            return pb
    return None


def _sensor_name(field_names: set[str]) -> str | None:
    low = {n.lower().replace("_", " ") for n in field_names if n}
    for sig, name in _SENSOR_HINTS:
        if any(any(s in n for n in low) for s in sig):
            return name
    return None


# ----------------------------------------------------------------- Auflisten


def list_dev_fields(data: bytes) -> list[dict]:
    """Developer-Felder gruppiert je CIQ-App:
    [{idx, uuid, name, fields:[{fdn, name, units, carriers, n, sample,
      looks_empty, reason}]}]. `looks_empty` = kein/konstanter Wert (vermutlich
    kein Sensor getragen). [] wenn keine vorhanden / Lesefehler."""
    apps: dict[int, dict] = {}
    samples: dict[tuple[int, int], list] = {}
    carriers: dict[tuple[int, int], set] = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fitdecode.FitReader(io.BytesIO(data)) as fr:
                for m in fr:
                    if not isinstance(m, fitdecode.FitDataMessage):
                        continue
                    if m.name == "developer_data_id":
                        d = {f.name: f.value for f in m.fields}
                        di = d.get("developer_data_index")
                        if di is None:
                            continue
                        a = apps.setdefault(di, {"idx": di, "uuid": None,
                                                 "app_version_raw": None, "fields": {}})
                        a["uuid"] = _uuid(d.get("application_id"))
                        a["app_version_raw"] = d.get("application_version")
                    elif m.name == "field_description":
                        d = {f.name: f.value for f in m.fields}
                        di = d.get("developer_data_index")
                        fdn = d.get("field_definition_number")
                        if di is None or fdn is None:
                            continue
                        a = apps.setdefault(di, {"idx": di, "uuid": None,
                                                 "app_version_raw": None, "fields": {}})
                        a["fields"][fdn] = {
                            "fdn": fdn,
                            "name": (d.get("field_name") or "").strip() or f"field {fdn}",
                            "units": (d.get("units") or "").strip(),
                        }
                    else:
                        for f in m.fields:
                            fld = getattr(f, "field", None)
                            if isinstance(fld, _ft.DevField):
                                key = (fld.dev_data_index, fld.def_num)
                                carriers.setdefault(key, set()).add(m.name)
                                lst = samples.setdefault(key, [])
                                if (len(lst) < _HEUR_MAX
                                        and isinstance(f.value, (int, float))):
                                    lst.append(float(f.value))
    except Exception as exc:  # noqa: BLE001
        log.warning("list_dev_fields read failed: %s", exc)
        return []

    out = []
    for di, a in sorted(apps.items()):
        fields = []
        for fdn, fd in sorted(a["fields"].items()):
            vals = samples.get((di, fdn), [])
            n = len(vals)
            if n == 0:
                empty, const, sample = True, False, None
            elif all(abs(v - vals[0]) < 1e-9 for v in vals):
                empty, const, sample = False, True, vals[0]
            else:
                empty, const, sample = False, False, vals[0]
            fields.append({
                **fd, "carriers": sorted(carriers.get((di, fdn), set())),
                "n": n, "sample": sample, "looks_empty": empty or const,
                "reason": "no data" if empty else ("constant value" if const else ""),
            })
        if not fields:
            continue
        name = _sensor_name({f["name"] for f in a["fields"].values()})
        out.append({
            "idx": di, "uuid": a["uuid"],
            "produced_by": _produced_by(a["uuid"], a.get("app_version_raw")),
            "name": (name or _UUID_NAMES.get((a["uuid"] or "")[:8])
                     or (a["uuid"][:8] if a["uuid"] else f"CIQ app {di}")),
            "fields": fields,
        })
    return out
