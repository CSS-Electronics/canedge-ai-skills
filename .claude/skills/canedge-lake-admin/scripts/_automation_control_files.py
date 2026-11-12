# VENDORED COPY - do not edit here. Source: canedge-mdftoparquet-automation/modules/control_files.py (v4.4.0,
# commit 11df453). Copied by the canedge-datalake-update release skill (CANedge Intro repo) at every automation release.
# Validation of the optional control files in the input bucket/container root (events.json, custom-messages.json,
# geofences.json, aggregations.json, backlog.json, dbc-groups.json, routing.json).
#
# All control files are optional: A missing file means the feature is off. A file that is present but invalid is
# reported line by line with the prefix 'CONTROL FILE ERROR' (or 'CONTROL FILE WARNING') and the processing fails,
# so the problem is visible in the logs instead of being skipped silently.
#
# The module only uses the Python standard library, so it runs in every build target (Amazon Lambda + Glue, Google
# Cloud Functions, Azure Function + Container App Jobs, Local) and can be copied as is into other tools (e.g. the
# CANedge AI workspace) to validate a control file before it is uploaded.
#
# Usage:
#   data, issues = parse("events.json", text)       # JSON syntax + content checks
#   issues = validate("events.json", data)          # content checks only
#   data = check("events.json", text, logger)       # parse + log issues + raise ControlFileError on errors
import json
import re
from datetime import datetime
from typing import NamedTuple

CONTROL_FILES_VERSION = "4.4.0"

CONTROL_FILES = ("events.json", "custom-messages.json", "geofences.json", "aggregations.json", "backlog.json",
                 "dbc-groups.json", "routing.json")

BUILTIN_CUSTOM_FUNCTIONS = ("combine_dtcs", "custom_geofences", "delta_distance", "resample")
AGGREGATION_METHODS = ("min", "max", "avg", "median", "sum", "first", "last", "delta_sum", "delta_sum_pos",
                       "delta_sum_neg")
ROUTING_CLOUDS = ("Amazon", "Local")

DEVICE_RE = re.compile(r"^[0-9A-F]{8}$")
DEVICE_ANY_CASE_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
TABLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")    # valid in Athena, BigQuery and Synapse
DBC_FILE_RE = re.compile(r"^(can|lin)[0-9]{1,2}-.+\.dbc$")   # can11-, can12-, ... = CANmod.router channels
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RASTER_RE = re.compile(r"^\d+(\.\d+)?([a-zA-Z]+)$")
RASTER_UNITS = ("ns", "us", "ms", "s", "min", "h", "D")
RASTER_OLD_UNITS = {"N": "ns", "U": "us", "L": "ms", "S": "s", "T": "min", "H": "h", "d": "D"}   # older pandas aliases
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
LOG_FILE_EXTENSIONS = (".MF4", ".MFC", ".MFE", ".MFM")


class Issue(NamedTuple):
    level: str      # "error" or "warning"
    path: str       # location in the file, e.g. events[0] (HighSpeedAlert).raster
    message: str

    def __str__(self):
        return f"{self.path}: {self.message}" if self.path else self.message


class ControlFileError(Exception):
    """A control file is present but invalid. Raised after all issues are logged."""

    def __init__(self, name, issues):
        self.name = name
        self.issues = [i for i in issues if i.level == "error"]
        super().__init__(f"{name} is invalid ({len(self.issues)} error(s)) - see the CONTROL FILE ERROR lines above")


# -----------------------------------------------------------
# Entry points
def parse(name, text, cloud=None, input_bucket=None):
    """Parse the file content (str or bytes) and validate it. Returns (data, issues); data is None on a syntax error."""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    text = text.lstrip("﻿")   # a BOM from e.g. Windows Notepad is not an error
    try:
        data = json.loads(text)
    except ValueError as e:
        line, col = getattr(e, "lineno", None), getattr(e, "colno", None)
        where = f" at line {line} column {col}" if line else ""
        return None, [Issue("error", "", f"invalid JSON{where}: {getattr(e, 'msg', str(e))}")]
    return data, validate(name, data, cloud, input_bucket)


def validate(name, data, cloud=None, input_bucket=None):
    """Validate parsed content. cloud ("Amazon", "Google", "Azure", "Local") and input_bucket are optional context."""
    validator = VALIDATORS.get(name)
    if validator is None:
        return [Issue("error", "", f"unknown control file - supported: {', '.join(CONTROL_FILES)}")]
    issues = []
    validator(data, _Reporter(issues), cloud=cloud, input_bucket=input_bucket)
    return issues


def report(name, issues, logger):
    """Log each issue with the CONTROL FILE prefix and raise ControlFileError if there are errors."""
    for issue in issues:
        if issue.level == "error":
            logger.error(f"CONTROL FILE ERROR: {name}: {issue}")
        else:
            logger.warning(f"CONTROL FILE WARNING: {name}: {issue}")
    if any(i.level == "error" for i in issues):
        raise ControlFileError(name, issues)


def check(name, text, logger, cloud=None, input_bucket=None):
    """parse() + report(): Returns the data of a valid file, raises ControlFileError otherwise."""
    data, issues = parse(name, text, cloud, input_bucket)
    report(name, issues, logger)
    return data


# -----------------------------------------------------------
# Helpers
class _Reporter:
    def __init__(self, issues):
        self.issues = issues

    def error(self, path, message):
        self.issues.append(Issue("error", path, message))

    def warning(self, path, message):
        self.issues.append(Issue("warning", path, message))


def _join(path, key):
    return f"{path}.{key}" if path else key


def _label(prefix, index, entry, name_key):
    name = entry.get(name_key) if isinstance(entry, dict) else None
    return f"{prefix}[{index}] ({name})" if isinstance(name, str) and name else f"{prefix}[{index}]"


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_str_list(v, allow_empty=False):
    return isinstance(v, list) and (allow_empty or len(v) > 0) and all(isinstance(x, str) and x for x in v)


def _require(r, obj, key, path, kind, check, hint):
    """Report a missing key or a wrong value. Returns the value if present and valid, else None."""
    if not isinstance(obj, dict) or key not in obj:
        r.error(_join(path, key), f"missing (required: {kind})")
        return None
    value = obj[key]
    if not check(value):
        r.error(_join(path, key), f"must be {hint}, got {json.dumps(value)}")
        return None
    return value


def _unknown_keys(r, obj, known, path):
    for key in obj:
        if key not in known:
            r.warning(_join(path, key), f"unknown key - ignored (known keys: {', '.join(known)})")


def _check_raster(r, obj, path):
    raster = _require(r, obj, "raster", path, "string", lambda v: isinstance(v, str),
                      'a string, e.g. "1s", "0.2s", "500ms" or "" (no resampling)')
    if raster:
        m = RASTER_RE.match(raster)
        if not m:
            r.error(_join(path, "raster"), f'"{raster}" is not a valid raster - use e.g. "1s", "0.2s", "500ms", "1min" '
                                           f'or "" (no resampling)')
        elif m.group(2) in RASTER_OLD_UNITS:
            r.warning(_join(path, "raster"), f'unit "{m.group(2)}" is an older alias - use "{RASTER_OLD_UNITS[m.group(2)]}"')
        elif m.group(2) not in RASTER_UNITS:
            r.error(_join(path, "raster"), f'unknown unit "{m.group(2)}" - use ms, s, min or h, e.g. "0.2s"')
    return raster


def _check_message_filter(r, entry, path, allowed_types):
    """messages_match_type + messages_filtered_list (events and custom messages)."""
    match_type = entry.get("messages_match_type") if isinstance(entry, dict) else None
    if "messages_match_type" not in entry:
        r.error(_join(path, "messages_match_type"), f"missing (required: one of {', '.join(allowed_types)})")
        return
    if match_type == "exact":
        r.error(_join(path, "messages_match_type"), '"exact" is not supported - use "equals"')
        return
    if match_type not in allowed_types:
        r.error(_join(path, "messages_match_type"), f"must be one of {', '.join(allowed_types)}, got "
                                                    f"{json.dumps(match_type)}")
        return
    key = _join(path, "messages_filtered_list")
    if "messages_filtered_list" not in entry:
        if match_type != "all_messages":
            r.error(key, "missing (required)")
        return
    value = entry["messages_filtered_list"]
    if match_type == "equals":
        if not (isinstance(value, list) and value and all(_is_str_list(x) for x in value)):
            r.error(key, 'with "equals" it must be a list of lists of message names, e.g. [["CAN9_GnssSpeed"]], '
                         f"got {json.dumps(value)}")
    elif match_type == "contains":
        if not (isinstance(value, str) and value):
            r.error(key, 'with "contains" it must be a non-empty string that message names contain, e.g. '
                         f'"_DM01_DTC_", got {json.dumps(value)}')


# -----------------------------------------------------------
# events.json
def _validate_events(data, r, **_):
    if not isinstance(data, dict):
        r.error("", 'must be an object with "events" (and optionally "general")')
        return
    _unknown_keys(r, data, ("general", "events"), "")

    general = data.get("general", {})
    if not isinstance(general, dict):
        r.error("general", "must be an object")
        general = {}
    include_gps = general.get("include_gps_data", True)
    for key, check, hint in (("include_gps_data", lambda v: isinstance(v, bool), "true or false"),
                             ("signal_latitude", lambda v: isinstance(v, str) and v, "a signal name"),
                             ("signal_longitude", lambda v: isinstance(v, str) and v, "a signal name"),
                             ("messages_gps", _is_str_list, 'a list of message names, e.g. ["CAN9_GnssPos"]'),
                             ("static_body_content", lambda v: isinstance(v, str), "a string")):
        if key in general and not check(general[key]):
            r.error(_join("general", key), f"must be {hint}, got {json.dumps(general[key])}")
    _unknown_keys(r, general, ("include_gps_data", "signal_latitude", "signal_longitude", "messages_gps",
                               "static_body_content"), "general")

    events = _require(r, data, "events", "", "list", lambda v: isinstance(v, list), "a list of event objects")
    if events is None:
        return
    if not events:
        r.warning("events", "is empty - no events are detected")
    names = set()
    for i, event in enumerate(events):
        path = _label("events", i, event, "event_name")
        if not isinstance(event, dict):
            r.error(path, "must be an object")
            continue
        name = _require(r, event, "event_name", path, "string", lambda v: isinstance(v, str) and v, "a non-empty string")
        if name:
            if "/" in name or "\\" in name:
                r.error(_join(path, "event_name"), "must not contain / or \\ (it is used in file names)")
            if name in names:
                r.warning(_join(path, "event_name"), "is used by more than one event")
            names.add(name)
        _check_message_filter(r, event, path, ("equals", "contains"))
        _require(r, event, "trigger_signals", path, "list", _is_str_list, 'a list of signal names, e.g. ["Speed"]')
        lower = _require(r, event, "lower_threshold", path, "number", _is_number, "a number")
        upper = _require(r, event, "upper_threshold", path, "number", _is_number, "a number")
        _require(r, event, "rising_as_start", path, "true/false", lambda v: isinstance(v, bool), "true or false")
        exact = _require(r, event, "exact_match", path, "true/false", lambda v: isinstance(v, bool), "true or false")
        raster = _check_raster(r, event, path)
        if lower is not None and upper is not None and exact is False and lower > upper:
            r.warning(path, f"lower_threshold ({lower}) is above upper_threshold ({upper}) - the event can never start")
        if include_gps and raster == "":
            r.warning(_join(path, "raster"), 'is "" - GPS positions are only added to events with a raster '
                                             '(e.g. "1s")')
        _unknown_keys(r, event, ("event_name", "messages_match_type", "messages_filtered_list", "trigger_signals",
                                 "lower_threshold", "upper_threshold", "rising_as_start", "exact_match", "raster"), path)


# -----------------------------------------------------------
# custom-messages.json
def _validate_custom_messages(data, r, **_):
    if not isinstance(data, list):
        r.error("", "must be a list of custom message objects")
        return
    names = set()
    for i, entry in enumerate(data):
        path = _label("", i, entry, "custom_message_name")
        if not isinstance(entry, dict):
            r.error(path, "must be an object")
            continue
        name = _require(r, entry, "custom_message_name", path, "string", lambda v: isinstance(v, str) and v,
                        "a non-empty string")
        if name:
            if not TABLE_NAME_RE.match(name):
                r.error(_join(path, "custom_message_name"), f'"{name}" must start with a letter and contain only '
                                                            "letters, digits and _ (it becomes a table name)")
            if name in names:
                r.error(_join(path, "custom_message_name"), "is used by more than one entry (the files would "
                                                            "overwrite each other)")
            names.add(name)
        _check_message_filter(r, entry, path, ("equals", "contains", "all_messages"))
        if entry.get("messages_match_type") == "all_messages":
            r.warning(_join(path, "messages_match_type"), '"all_messages" is meant for ad hoc use - the table schema '
                                                          "varies between log files")
        raster = _check_raster(r, entry, path)
        _require(r, entry, "prefix", path, "true/false", lambda v: isinstance(v, bool), "true or false")
        function = _require(r, entry, "function", path, "string", lambda v: isinstance(v, str) and v,
                            "a function name, e.g. " + ", ".join(BUILTIN_CUSTOM_FUNCTIONS))
        if function and function not in BUILTIN_CUSTOM_FUNCTIONS:
            r.warning(_join(path, "function"), f'"{function}" is not a built-in function '
                                               f"({', '.join(BUILTIN_CUSTOM_FUNCTIONS)}) - it needs a customized "
                                               "custom_message_functions.py, otherwise the data is passed through as is")
        lists = entry.get("messages_filtered_list")
        if raster == "" and entry.get("messages_match_type") == "equals" and isinstance(lists, list) \
                and any(isinstance(x, list) and len(x) > 1 for x in lists):
            r.warning(_join(path, "raster"), 'is "" with several messages per set - their rows are stacked, not '
                                             'joined (set a raster, e.g. "1s", to combine signals)')
        _unknown_keys(r, entry, ("custom_message_name", "messages_match_type", "messages_filtered_list", "raster",
                                 "prefix", "function"), path)


# -----------------------------------------------------------
# geofences.json
def _validate_geofences(data, r, **_):
    if not isinstance(data, list):
        r.error("", "must be a list of geofence objects")
        return
    ids = set()
    for i, fence in enumerate(data):
        path = _label("", i, fence, "name")
        if not isinstance(fence, dict):
            r.error(path, "must be an object")
            continue
        fid = _require(r, fence, "id", path, "integer", lambda v: isinstance(v, int) and not isinstance(v, bool),
                       "an integer")
        if fid is not None:
            if fid == 0:
                r.warning(_join(path, "id"), "0 is also the value for 'outside all geofences'")
            if fid in ids:
                r.warning(_join(path, "id"), "is used by more than one geofence")
            ids.add(fid)
        _require(r, fence, "name", path, "string", lambda v: isinstance(v, str), "a string")
        _require(r, fence, "latitude", path, "number", lambda v: _is_number(v) and -90 <= v <= 90,
                 "a number from -90 to 90")
        _require(r, fence, "longitude", path, "number", lambda v: _is_number(v) and -180 <= v <= 180,
                 "a number from -180 to 180")
        _require(r, fence, "radius", path, "number", lambda v: _is_number(v) and v > 0,
                 "a number above 0 (kilometers)")
        _unknown_keys(r, fence, ("id", "name", "latitude", "longitude", "radius"), path)


# -----------------------------------------------------------
# aggregations.json
def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d") if isinstance(value, str) and DATE_RE.match(value) else None
    except ValueError:
        return None


def _validate_aggregations(data, r, **_):
    if not isinstance(data, dict):
        r.error("", 'must be an object with "config", "device_clusters" and "cluster_details"')
        return
    _unknown_keys(r, data, ("config", "device_clusters", "cluster_details"), "")
    config = _require(r, data, "config", "", "object", lambda v: isinstance(v, dict), "an object")
    if config is not None:
        date = _require(r, config, "date", "config", "object", lambda v: isinstance(v, dict), "an object")
        if date is not None:
            mode = _require(r, date, "mode", "config.date", "string", lambda v: v in ("previous_day", "specific_period"),
                            '"previous_day" or "specific_period"')
            if mode == "specific_period":
                start = _require(r, date, "start_date", "config.date", "YYYY-MM-DD", lambda v: _parse_date(v),
                                 "a date in the format YYYY-MM-DD")
                end = _require(r, date, "end_date", "config.date", "YYYY-MM-DD", lambda v: _parse_date(v),
                               "a date in the format YYYY-MM-DD")
                if start and end and _parse_date(end) < _parse_date(start):
                    r.error("config.date", f"end_date {end} is before start_date {start}")
        trip = _require(r, config, "trip", "config", "object", lambda v: isinstance(v, dict), "an object")
        if trip is not None:
            for key in ("trip_gap_min", "trip_min_length_min"):
                _require(r, trip, key, "config.trip", "number of minutes", lambda v: _is_number(v) and v >= 0,
                         "a number of minutes (0 or more)")

    clusters = _require(r, data, "device_clusters", "", "list", lambda v: isinstance(v, list) and v,
                        "a non-empty list of device clusters")
    cluster_names = []
    for i, cluster in enumerate(clusters or []):
        path = _label("device_clusters", i, cluster, "cluster")
        if not isinstance(cluster, dict):
            r.error(path, "must be an object")
            continue
        name = _require(r, cluster, "cluster", path, "string", lambda v: isinstance(v, str) and v, "a cluster name")
        if name:
            cluster_names.append(name)
        devices = _require(r, cluster, "devices", path, "list", lambda v: isinstance(v, list), "a list of device IDs")
        if devices is not None:
            if not devices:
                r.error(_join(path, "devices"), "is empty - list the device IDs to process")
            for device in devices:
                if not (isinstance(device, str) and DEVICE_RE.match(device)):
                    r.error(_join(path, "devices"), f"{json.dumps(device)} is not a device ID (8 upper-case hex "
                                                    "characters, e.g. 2F6913DB)")

    details_clusters = set()
    for i, detail in enumerate(_require(r, data, "cluster_details", "", "list", lambda v: isinstance(v, list) and v,
                                        "a non-empty list") or []):
        path = f"cluster_details[{i}]"
        if not isinstance(detail, dict):
            r.error(path, "must be an object")
            continue
        names = _require(r, detail, "clusters", path, "list", _is_str_list, "a list of cluster names")
        details_clusters.update(names or [])
        details = _require(r, detail, "details", path, "object", lambda v: isinstance(v, dict), "an object")
        if details is None:
            continue
        path = _join(path, "details")
        trip_id = _require(r, details, "trip_identifier", path, "object", lambda v: isinstance(v, dict),
                           'an object, e.g. {"message": "CAN9_GnssSpeed"}')
        if trip_id is not None:
            _require(r, trip_id, "message", _join(path, "trip_identifier"), "string",
                     lambda v: isinstance(v, str) and v, "a message name")
        aggs = _require(r, details, "aggregations", path, "list", lambda v: isinstance(v, list) and v,
                        "a non-empty list")
        for j, agg in enumerate(aggs or []):
            apath = _label(_join(path, "aggregations"), j, agg, "message")
            if not isinstance(agg, dict):
                r.error(apath, "must be an object")
                continue
            _require(r, agg, "message", apath, "string", lambda v: isinstance(v, str) and v, "a message name")
            _require(r, agg, "signal", apath, "list", _is_str_list, 'a list of signal names, e.g. ["Speed"]')
            methods = _require(r, agg, "aggregation", apath, "list", _is_str_list,
                               'a list of aggregation methods, e.g. ["avg", "max"]')
            for method in methods or []:
                if method not in AGGREGATION_METHODS:
                    r.error(_join(apath, "aggregation"), f'"{method}" is not supported - use: '
                                                         f"{', '.join(AGGREGATION_METHODS)}")
            _unknown_keys(r, agg, ("message", "signal", "aggregation"), apath)

    for name in cluster_names:
        if details_clusters and name not in details_clusters:
            r.error("device_clusters", f'cluster "{name}" has no entry in cluster_details - its devices are skipped')


# -----------------------------------------------------------
# backlog.json
def classify_backlog_entry(entry):
    """Return (kind, normalized entry) like the backlog processing does: kind is "device", "session", "file" or None
    (ignored). Device IDs are upper-cased and prefixes get a trailing slash."""
    if not isinstance(entry, str) or not entry.strip():
        return None, entry
    parts = entry.strip().strip("/").split("/")
    if DEVICE_ANY_CASE_RE.match(parts[0]):
        parts[0] = parts[0].upper()
    normalized = "/".join(parts)
    if entry.upper().endswith(LOG_FILE_EXTENSIONS) and len(parts) >= 3:
        return "file", normalized
    if len(parts) == 1 and DEVICE_RE.match(parts[0]):
        return "device", normalized + "/"
    if len(parts) == 2 and DEVICE_RE.match(parts[0]) and re.match(r"^[0-9]{8}$", parts[1]):
        return "session", normalized + "/"
    return None, entry


def _validate_backlog(data, r, **_):
    if not isinstance(data, dict):
        r.error("", 'must be an object with "config" and "files"')
        return
    _unknown_keys(r, data, ("config", "files"), "")
    config = _require(r, data, "config", "", "object", lambda v: isinstance(v, dict), "an object")
    batch = _require(r, config, "batch_size", "config", "object", lambda v: isinstance(v, dict),
                     'an object, e.g. {"min": 10, "max": 256}') if config is not None else None
    if batch is not None:
        is_count = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1  # noqa: E731
        low = _require(r, batch, "min", "config.batch_size", "integer", is_count, "an integer of 1 or more")
        high = _require(r, batch, "max", "config.batch_size", "integer", is_count, "an integer of 1 or more")
        if low and high and low > high:
            r.error("config.batch_size", f"min ({low}) is above max ({high})")
    files = _require(r, data, "files", "", "list", lambda v: isinstance(v, list), "a list of device, session or "
                                                                                  "log file paths")
    if files is None:
        return
    if not files:
        r.error("files", "is empty - nothing to process")
    for i, entry in enumerate(files):
        kind, normalized = classify_backlog_entry(entry)
        if kind is None:
            r.warning(f"files[{i}]", f"{json.dumps(entry)} is ignored - use a device ('2F6913DB/'), a session "
                                     "('2F6913DB/00000001/') or a log file key ('2F6913DB/00000001/00000001.MF4')")
        elif normalized.rstrip("/") != entry.strip().strip("/"):
            r.warning(f"files[{i}]", f'"{entry}" is read as "{normalized}"')


# -----------------------------------------------------------
# dbc-groups.json
def _validate_dbc_groups(data, r, **_):
    if not isinstance(data, dict):
        r.error("", 'must be an object with "dbc_groups"')
        return
    groups = _require(r, data, "dbc_groups", "", "list", lambda v: isinstance(v, list), "a list of groups")
    seen = {}
    for i, group in enumerate(groups or []):
        path = f"dbc_groups[{i}]"
        if not isinstance(group, dict):
            r.error(path, "must be an object")
            continue
        devices = _require(r, group, "devices", path, "list", _is_str_list, "a non-empty list of device IDs")
        for device in devices or []:
            if not DEVICE_RE.match(device):
                r.error(_join(path, "devices"), f'"{device}" is not a device ID (8 upper-case hex characters, '
                                                "e.g. 2F6913DB)")
            elif device in seen:
                r.warning(_join(path, "devices"), f"{device} is also in dbc_groups[{seen[device]}] - the first "
                                                  "group wins")
            else:
                seen[device] = i
        files = _require(r, group, "dbc_files", path, "list", _is_str_list, "a non-empty list of DBC file names")
        for name in files or []:
            if not DBC_FILE_RE.match(name):
                r.error(_join(path, "dbc_files"), f'"{name}" must be a bucket root DBC file named like '
                                                  "can1-<name>.dbc, can11-<name>.dbc or lin1-<name>.dbc (lower case)")
        _unknown_keys(r, group, ("devices", "dbc_files"), path)
    # Optional (v4.4.0+): DBC files added to the files of every device that is in a group
    if "common_dbc_files" in data:
        common = data["common_dbc_files"]
        if not _is_str_list(common):
            r.error("common_dbc_files", f"must be a non-empty list of DBC file names, got {json.dumps(common)}")
        else:
            for name in common:
                if not DBC_FILE_RE.match(name):
                    r.error("common_dbc_files", f'"{name}" must be a bucket root DBC file named like '
                                                "can1-<name>.dbc, can11-<name>.dbc or lin1-<name>.dbc (lower case)")
            if isinstance(groups, list) and not groups:
                r.warning("common_dbc_files", "no effect without dbc_groups (devices in no group get all DBC files)")
    _unknown_keys(r, data, ("dbc_groups", "common_dbc_files"), "")


# -----------------------------------------------------------
# routing.json
def _validate_routing(data, r, cloud=None, input_bucket=None, **_):
    if not isinstance(data, dict):
        r.error("", 'must be an object with "config" and "devices"')
        return
    if cloud and cloud not in ROUTING_CLOUDS:
        r.warning("", f"routing.json has no effect on {cloud} (supported on Amazon only)")
    _unknown_keys(r, data, ("config", "devices"), "")
    config = data.get("config", {})
    if not isinstance(config, dict):
        r.error("config", "must be an object")
    elif "mirror_to_default" in config and not isinstance(config["mirror_to_default"], bool):
        r.error("config.mirror_to_default", f"must be true or false (without quotes), got "
                                            f"{json.dumps(config['mirror_to_default'])}")
    devices = _require(r, data, "devices", "", "object", lambda v: isinstance(v, dict),
                       'an object mapping device IDs to rules, e.g. {"2F6913DB": {...}}')
    # Rule-level problems are warnings: the function then uses the default output bucket for that device (catch-all)
    for device, rule in (devices or {}).items():
        path = f"devices.{device}"
        if not DEVICE_RE.match(device):
            r.warning(path, "is not a device ID (8 upper-case hex characters) - it never matches")
        if not isinstance(rule, dict):
            r.warning(path, "must be an object - the device uses the default output bucket")
            continue
        bucket = rule.get("output_bucket")
        if not (isinstance(bucket, str) and bucket):
            r.warning(_join(path, "output_bucket"), "missing - the device uses the default output bucket")
        elif not (BUCKET_RE.match(bucket) and bucket.endswith("-parquet")):
            r.warning(_join(path, "output_bucket"), f'"{bucket}" should be named <input-bucket>-<client>-parquet '
                                                    "(lower case)")
        elif input_bucket and not bucket.startswith(f"{input_bucket}-"):
            r.warning(_join(path, "output_bucket"), f'"{bucket}" does not start with "{input_bucket}-" - the function '
                                                    "may not be allowed to write to it")
        from_date = rule.get("from_date", "")
        if from_date and not _parse_date(from_date):
            r.warning(_join(path, "from_date"), f"{json.dumps(from_date)} is not a YYYY-MM-DD date - the device uses "
                                                "the default output bucket")
        _unknown_keys(r, rule, ("output_bucket", "from_date"), path)


VALIDATORS = {
    "events.json": _validate_events,
    "custom-messages.json": _validate_custom_messages,
    "geofences.json": _validate_geofences,
    "aggregations.json": _validate_aggregations,
    "backlog.json": _validate_backlog,
    "dbc-groups.json": _validate_dbc_groups,
    "routing.json": _validate_routing,
}
