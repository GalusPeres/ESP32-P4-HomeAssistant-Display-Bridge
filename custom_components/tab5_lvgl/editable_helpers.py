"""Bounded contracts for writable values; independent of Home Assistant imports."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo

NUMBER_DOMAINS = ("number", "input_number")
SELECT_DOMAINS = ("select", "input_select")
DATETIME_DOMAINS = ("time", "date", "datetime", "input_datetime")
EDITABLE_DOMAINS = NUMBER_DOMAINS + SELECT_DOMAINS + DATETIME_DOMAINS
EDITABLE_LISTS = {"numbers": NUMBER_DOMAINS, "selects": SELECT_DOMAINS,
                  "datetimes": DATETIME_DOMAINS}
MAX_OPTIONS = 64
MAX_OPTION_BYTES = 255
MAX_CONTROL_BYTES = 24576


def domain_of(entity):
    if not isinstance(entity, str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity):
        return ""
    return entity.split(".", 1)[0]


def editable_selection(value, domains):
    if not isinstance(value, list):
        raise ValueError("invalid_editable_entities")
    if any(domain_of(item) not in domains for item in value):
        raise ValueError("invalid_editable_entities")
    return list(dict.fromkeys(value))


def finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def build_editable_payload(entity, state, attributes, session, time_zone="UTC"):
    domain = domain_of(entity)
    kind = ("number" if domain in NUMBER_DOMAINS else
            "select" if domain in SELECT_DOMAINS else domain)
    if domain == "input_datetime":
        kind = ("datetime" if attributes.get("has_date") and attributes.get("has_time")
                else "date" if attributes.get("has_date") else "time")
    available = state is not None and state != "unavailable"
    value = state if isinstance(state, str) else None
    payload = {"version": 1, "kind": kind, "state": value,
               "available": available, "session": session, "writable": available}
    if kind == "number":
        for key in ("min", "max", "step"):
            payload[key] = finite_number(attributes.get(key))
        lo, hi, step = (payload[key] for key in ("min", "max", "step"))
        payload["writable"] = bool(available and None not in (lo, hi, step)
                                   and lo < hi and step > 0)
        mode = attributes.get("mode", "auto")
        payload["mode"] = mode if mode in ("auto", "slider", "box") else "auto"
        payload["unit"] = str(attributes.get("unit_of_measurement") or "")[:32]
    elif kind == "select":
        options = attributes.get("options")
        complete = (isinstance(options, (list, tuple)) and 0 < len(options) <= MAX_OPTIONS
                    and all(isinstance(option, str) and option
                            and "\n" not in option and "\r" not in option
                            and len(option.encode("utf-8")) <= MAX_OPTION_BYTES
                            for option in options)
                    and len(set(options)) == len(options))
        payload["options_complete"] = bool(complete)
        payload["options"] = list(options) if complete else []
        payload["writable"] = bool(available and complete)
    elif kind in ("date", "time", "datetime"):
        if domain == "input_datetime":
            payload["writable"] = bool(available and
                                       (attributes.get("has_date") is True or attributes.get("has_time") is True))
        payload["time_zone"] = time_zone
        if kind == "datetime" and value not in (None, "unknown", "unavailable"):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(ZoneInfo(time_zone))
                payload["state"] = parsed.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, KeyError):
                payload["state"] = "unknown"
    else:
        payload["writable"] = False
    constraints = {key: item for key, item in payload.items() if key != "state"}
    payload["revision"] = hashlib.sha256(json.dumps(constraints, sort_keys=True,
                                                   ensure_ascii=False).encode()).hexdigest()[:16]
    return payload


def build_editable_service_call(entity, value, payload, time_zone="UTC"):
    """Validate against current capabilities, never a device-provided service."""
    domain = domain_of(entity)
    if domain not in EDITABLE_DOMAINS or not payload.get("writable"):
        raise ValueError("unavailable")
    kind = payload["kind"]
    if kind == "number":
        numeric = finite_number(value)
        if numeric is None or not payload["min"] <= numeric <= payload["max"]:
            raise ValueError("invalid_value")
        try:
            steps = ((Decimal(str(value)) - Decimal(str(payload["min"])))
                     / Decimal(str(payload["step"])))
            nearest = steps.to_integral_value()
            if abs(steps - nearest) > Decimal("0.000001"):
                raise ValueError("invalid_step")
            numeric = float(Decimal(str(payload["min"])) + nearest * Decimal(str(payload["step"])))
        except (InvalidOperation, ZeroDivisionError):
            raise ValueError("invalid_value") from None
        return domain, "set_value", {"value": numeric}
    if kind == "select":
        if not isinstance(value, str) or value not in payload["options"]:
            raise ValueError("invalid_option")
        return domain, "select_option", {"option": value}
    if not isinstance(value, str):
        raise ValueError("invalid_value")
    try:
        if kind == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError()
            canonical = date.fromisoformat(value).isoformat()
        elif kind == "time":
            if not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
                raise ValueError()
            canonical = time.fromisoformat(value).isoformat(timespec="seconds")
        else:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?", value):
                raise ValueError()
            local = datetime.fromisoformat(value)
            zone = ZoneInfo(time_zone)
            candidates = [local.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
            valid = [candidate for candidate in candidates if
                     candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == local]
            if not valid or len({candidate.utcoffset() for candidate in valid}) != 1:
                raise ValueError("ambiguous_time")
            canonical = valid[0].isoformat(timespec="seconds")
    except (ValueError, OverflowError, KeyError):
        raise ValueError("invalid_value") from None
    if domain == "input_datetime":
        if kind == "datetime":
            return domain, "set_datetime", {"timestamp": valid[0].timestamp()}
        return domain, "set_datetime", {kind: canonical}
    return domain, "set_value", {kind: canonical}


def add_number_history(response, states, start, end, points, *, complete=True, complete_until=None):
    """Sample the actual step state, preserving unknown/unavailable gaps."""
    points = max(1, min(288, points))
    records = []
    for state in states:
        stamp = getattr(state, "last_changed", None)
        raw = getattr(state, "state", None)
        if isinstance(state, dict):
            stamp = state.get("last_changed", state.get("last_updated"))
            raw = state.get("state")
        if isinstance(stamp, str):
            try:
                stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
        if isinstance(stamp, datetime):
            stamp = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)
            records.append((stamp.timestamp(), finite_number(raw)))
    if not complete:
        cutoff = complete_until.timestamp() if isinstance(complete_until, datetime) else start.timestamp()
        # Missing Recorder pages must never become a continuous numeric value.
        records.append((cutoff, None))
    records.sort(key=lambda item: item[0])
    values = []
    index = 0
    previous = None
    span = (end - start).total_seconds()
    for point in range(points):
        timestamp = start.timestamp() + span * point / max(points - 1, 1)
        while index < len(records) and records[index][0] <= timestamp:
            previous = records[index][1]
            index += 1
        values.append(previous)
    response.update(kind="number", values=values, period_minutes=5 if response["hours"] == 24 else 60)
    for key in ("palette", "segments", "timeline_data", "timeline_encoding", "timeline_points"):
        response.pop(key, None)
    return response
