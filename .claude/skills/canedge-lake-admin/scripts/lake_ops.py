r"""Monitor and troubleshoot the data lake automation (canedge-lake-admin): Function logs, job runs, one log file's path.

  status                                  Automation version, root control files, log/job access, function
                                          results in the last 24 h, last run per job
  logs  --source S [--since 24h] [--grep TEXT] [--key KEY] [--run ID] [--errors] [--max N]
                                          Condensed log lines, grouped per invocation/run. S: lambda (the per-file
                                          function, also on Google), backlog, aggregation, map-tables
  diagnose KEY                            Why a log file is (not) in the lake: Object + trigger suffix -> function
                                          invocation(s) and result -> Parquet output -> catalog tables
  runs  [--job J] [--last N]              Job runs: State, duration, error (AWS Glue; Google: the HTTP requests)
  start --job J [--timeout MIN] [--wait]  Start backlog / aggregation / map-tables. ONLY on the user's request: Checks
                                          the job's JSON first and that no run is active (the harness asks before).
                                          --timeout (AWS): This run's limit in minutes instead of the job's (max 2880)

AWS: CloudWatch Logs /aws/lambda/mdf-to-parquet-lambda-function-<id> and /aws-glue/canedge-<id>/(output|error), Glue
jobs <id>-map-tables|process-backlog|process-aggregation (the data lake stack's programmatic access keys, stack with
ProgrammaticAccessPolicy-<id>). Google: Cloud Logging view canedge-<id> (programmatic access key file), services
<id>-mdf-to-parquet|mdf-to-parquet-backlog|aggregation|bq-map-tables. Times are UTC.

Usage:
  python lake_ops.py status
  python lake_ops.py diagnose 2F6913DB/00000086/00000003-62977DFB.MF4
  python lake_ops.py logs --source lambda --since 6h --errors
  python lake_ops.py runs --job backlog
Options: --env NAME (data lake profile), --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
for _p in (Path(__file__).resolve().parent, SKILLS / "canedge-workspace" / "scripts", SKILLS / "canedge-s3" / "scripts",
           SKILLS / "canedge-lake-query" / "scripts"):
    sys.path.insert(0, str(_p))
import _automation_control_files as acf  # noqa: E402
from env import aws_client, lake, load_env, require_s3, use_env  # noqa: E402

UTC = dt.timezone.utc
JOBS = {"backlog": ("process-backlog", "backlog.json"), "aggregation": ("process-aggregation", "aggregations.json"),
        "map-tables": ("map-tables", None)}
GOOGLE_SERVICES = {"lambda": "mdf-to-parquet", "backlog": "mdf-to-parquet-backlog", "aggregation": "aggregation",
                   "map-tables": "bq-map-tables"}
TRIGGER_SUFFIXES = (".MF4", ".MFC", ".MFE", ".MFM")   # S3 event filters (case-sensitive)
SECTIONS = ("DOWNLOAD OBJECTS", "DBC DECODE MDF TO PARQUET", "ADD CUSTOM MESSAGES", "DETECT EVENTS",
            "LOAD ROUTING CONFIG", "DO FINAL PROCESSING")
LAMBDA_LINE = re.compile(r"^\[(\w+)\]\s+\S+\s+([0-9a-f-]{36})\s+(.*)$", re.S)
REQUEST_ID = re.compile(r"RequestId: ([0-9a-f-]{36})")
# Routine per-file lines, hidden by logs unless --all (they can be hundreds per invocation/run)
NOISE = re.compile(r"Uploaded object to|Successfully downloaded|Listed objects in|Downloaded \S+ to|Copied \S+ from|"
                   r"Skipped (file|directory)|Listing contents of|- INFO - \[|- INFO -\s*$|"
                   r"^\d\d/\d\d/\d\d [\d:]+ (INFO|WARN) |"
                   r"^\S* ?INFO\s+\d{4}-\d\d-\d\dT[\d:,]+\s+\d+\s+(com\.amazonaws|org\.apache)")


class OpsError(Exception):
    pass


def since_ms(spec: str) -> int:
    """'24h', '30m', '7d' or an ISO time -> epoch ms."""
    m = re.fullmatch(r"(\d+)([mhd])", spec)
    if m:
        delta = dt.timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]: int(m.group(1))})
        return int((dt.datetime.now(UTC) - delta).timestamp() * 1000)
    t = dt.datetime.fromisoformat(spec.replace("Z", "+00:00"))
    return int((t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp() * 1000)


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")


def result_of(lines: list[str]) -> dict:
    """Outcome of one function invocation from its log lines."""
    text = "\n".join(lines)
    section = None
    for line in lines:
        section = next((s for s in SECTIONS if s in line), section)
    if "CONTROL FILE ERROR" in text:
        res = "failed: invalid control file"
    elif "Execution succeeded (no Parquet files decoded)" in text:
        res = "succeeded, but no Parquet files decoded (no DBC message matched)"
    elif "Execution succeeded" in text:
        res = "succeeded"
    elif "Task timed out" in text:
        res = "failed: timeout"
    elif "Runtime.OutOfMemory" in text or "Runtime exited" in text:
        res = "failed: out of memory / crashed"
    elif "Execution failed" in text or "Traceback" in text:
        res = "failed"
    else:
        res = "unknown (no result line - still running, or the log is incomplete)"
    errors = [ln for ln in lines if re.search(r"\b(ERROR|CONTROL FILE ERROR)\b|Traceback|Error:", ln)
              and "not found" not in ln][:6]
    return {"result": res, "last_section": section, "errors": errors}


RESULT_PATTERN = '?"statusCode" ?"Task timed out" ?"Runtime exited" ?Traceback'


def result_counts(lines: list[str]) -> dict:
    """Invocations of the last 24 h by result, from the per-invocation result lines."""
    counts = {"succeeded": 0, "succeeded, no Parquet decoded": 0, "failed": 0, "timeout/crash": 0}
    for ln in lines:
        if "no Parquet files decoded" in ln:
            counts["succeeded, no Parquet decoded"] += 1
        elif "Execution succeeded" in ln:
            counts["succeeded"] += 1
        elif "Traceback" in ln:   # every failed run ends with one (also 'Execution failed', which raises)
            counts["failed"] += 1
        elif "Task timed out" in ln or "Runtime exited" in ln:
            counts["timeout/crash"] += 1
    return {"invocations": sum(counts.values()), "results": {k: v for k, v in counts.items() if v}}


# ---------------------------------------------------------------- AWS
class AwsOps:
    def __init__(self):
        require_s3(athena=True, admin=True)
        self.info = lake()
        self.id = self.info["lake_id"]
        self.logs_client, self.glue = aws_client("logs"), aws_client("glue")
        self.lambda_group = f"/aws/lambda/mdf-to-parquet-lambda-function-{self.id}"
        self.glue_groups = [f"/aws-glue/canedge-{self.id}/output", f"/aws-glue/canedge-{self.id}/error"]
        self.missing: set[str] = set()   # log groups that do not exist (yet): created at the first run

    def _filter(self, group: str, start_ms: int, end_ms: int | None = None, pattern: str = "",
                stream_prefix: str | None = None, limit: int = 2000, stream: str | None = None) -> list[dict]:
        kw = {"logGroupName": group, "startTime": start_ms, **({"endTime": end_ms} if end_ms else {}),
              **({"filterPattern": pattern} if pattern else {}),
              **({"logStreamNamePrefix": stream_prefix} if stream_prefix else {}),
              **({"logStreamNames": [stream]} if stream else {})}
        out, token = [], None
        try:
            while len(out) < limit:
                r = self.logs_client.filter_log_events(**kw, **({"nextToken": token} if token else {}))
                out += r.get("events", [])
                token = r.get("nextToken")
                if not token:
                    break
        except self.logs_client.exceptions.ResourceNotFoundException:
            self.missing.add(group)
            return []
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "response", {}).get("Error", {}).get("Code", e.__class__.__name__)
            if code in ("AccessDeniedException", "AccessDenied"):
                raise OpsError(f"No access to the log group {group} - the keys are not the data lake stack's "
                               "programmatic access keys, or the stack predates ProgrammaticAccessPolicy-<id>: Update "
                               "the stack (CANedge Intro: 'Update your CloudFormation stack revision')") from e
            raise
        return sorted(out[:limit], key=lambda e: e["timestamp"])

    @staticmethod
    def request_id(msg: str) -> str | None:
        m = LAMBDA_LINE.match(msg) or REQUEST_ID.search(msg)
        return (m.group(2) if m.re is LAMBDA_LINE else m.group(1)) if m else None

    def invocations(self, start_ms: int, pattern: str = "", end_ms: int | None = None, limit: int = 2000) -> dict:
        """Lambda log lines grouped per invocation (with a pattern: only invocations with a matching line). Lines
        without a request ID (e.g. the runtime's '[ERROR] ... Traceback' of an unhandled exception) belong to the
        invocation running in the same log stream at that time (a stream runs one invocation at a time)."""
        hits = self._filter(self.lambda_group, start_ms, end_ms, pattern, limit=limit)
        if not pattern:
            return self.group(hits)
        ids = list(dict.fromkeys(r for e in hits if (r := self.request_id(e["message"]))))
        out = {}
        for rid in ids[-10:]:
            own = self._filter(self.lambda_group, start_ms - 60_000, end_ms, f'"{rid}"', limit=500)
            if not own:
                continue
            full = self._filter(self.lambda_group, own[0]["timestamp"], own[-1]["timestamp"] + 1, limit=2000,
                                stream=own[0]["logStreamName"])
            out[rid] = [(e["timestamp"], self.clean(e["message"])) for e in full or own]
        return out

    def group(self, events: list[dict]) -> dict:
        out, current = {}, {}
        for e in sorted(events, key=lambda e: (e.get("logStreamName", ""), e["timestamp"])):
            rid = self.request_id(e["message"]) or current.get(e.get("logStreamName"), "-")
            current[e.get("logStreamName")] = rid
            out.setdefault(rid, []).append((e["timestamp"], self.clean(e["message"])))
        return dict(sorted(out.items(), key=lambda kv: kv[1][0][0]))

    @staticmethod
    def clean(msg: str) -> str:
        m = LAMBDA_LINE.match(msg.rstrip())
        text = f"{m.group(1)} {m.group(3)}".strip() if m else msg.strip()
        return " | ".join(x.strip() for x in text.replace(" ", " ").splitlines() if x.strip())

    def results_24h(self) -> dict:
        """Count the result lines of the per-file function (one per invocation) instead of reading every line."""
        lines = [e["message"] for e in self._filter(self.lambda_group, since_ms("24h"), pattern=RESULT_PATTERN,
                                                    limit=50_000)]
        if self.lambda_group in self.missing:
            return {"invocations": 0, "results": {}, "note": f"no log group {self.lambda_group}: the function has "
                                                             "never run (or its logs were deleted)"}
        return result_counts(lines)

    def job_name(self, job: str) -> str:
        return f"{self.id}-{JOBS[job][0]}"

    def runs(self, job: str, last: int = 5) -> list[dict]:
        try:
            runs = self.glue.get_job_runs(JobName=self.job_name(job), MaxResults=last)["JobRuns"]
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "response", {}).get("Error", {}).get("Code", e.__class__.__name__)
            raise OpsError(f"Glue job {self.job_name(job)}: {code}" + (
                " - update the stack to get job access (ProgrammaticAccessPolicy-<id>)" if "AccessDenied" in code
                else "")) from e
        return [{"id": r["Id"], "state": r["JobRunState"], "started": r["StartedOn"].astimezone(UTC).strftime(
                 "%Y-%m-%d %H:%M:%S"), "duration_s": r.get("ExecutionTime"), "error": r.get("ErrorMessage"),
                 "trigger": r.get("TriggerName") or "manual/API"} for r in runs]

    def run_lines(self, run_id: str, start_ms: int) -> list[tuple[int, str]]:
        """A Glue run's output + error log, one entry per text line (Glue writes multi-line events)."""
        lines = []
        for g in self.glue_groups:
            for e in self._filter(g, start_ms, stream_prefix=run_id, limit=20_000):
                lines += [(e["timestamp"], x.strip()) for x in e["message"].splitlines() if x.strip()]
        return sorted(lines, key=lambda x: x[0])

    def start(self, job: str, timeout: int | None = None) -> dict:
        name = self.job_name(job)
        r = self.glue.start_job_run(JobName=name, **({"Timeout": timeout} if timeout else {}))
        res = {"job": name, "run_id": r["JobRunId"]}
        try:
            res["timeout_min"] = timeout or self.glue.get_job(JobName=name)["Job"].get("Timeout")
        except Exception:  # noqa: BLE001 - informational only
            pass
        return res


# ---------------------------------------------------------------- Google
class GoogleOps:
    def __init__(self):
        require_s3(admin=True)
        load_env()
        import os
        from google.oauth2 import service_account
        self.info = lake()
        self.id = self.info["lake_id"]
        self.key_file = os.environ.get("CANEDGE_GOOGLE_KEY_FILE")
        if not (self.key_file and self.id):
            raise OpsError("This Google profile has no programmatic access key file - re-run setup_env.py --key-file")
        self.creds = service_account.Credentials.from_service_account_file(
            self.key_file, scopes=["https://www.googleapis.com/auth/logging.read"])
        self.project = self.creds.project_id
        self.view = f"projects/{self.project}/locations/global/buckets/_Default/views/canedge-{self.id}"

    def entries(self, flt: str, limit: int = 2000, newest_first: bool = True) -> list[dict]:
        from google.auth.transport.requests import AuthorizedSession
        s, out, token = AuthorizedSession(self.creds), [], None
        while len(out) < limit:
            body = {"resourceNames": [self.view], "filter": flt, "pageSize": min(1000, limit - len(out)),
                    "orderBy": "timestamp desc" if newest_first else "timestamp asc",
                    **({"pageToken": token} if token else {})}
            r = s.post("https://logging.googleapis.com/v2/entries:list", json=body, timeout=60)
            if r.status_code == 403:
                raise OpsError(f"No access to the log view canedge-{self.id} (HTTP 403)")
            r.raise_for_status()
            d = r.json()
            out += d.get("entries", [])
            token = d.get("nextPageToken")
            if not token:
                break
        return out

    def flt(self, source: str, start_ms: int, extra: str = "") -> str:
        svc = f"{self.id}-{GOOGLE_SERVICES[source]}"
        t = dt.datetime.fromtimestamp(start_ms / 1000, UTC).isoformat()
        return f'resource.labels.service_name="{svc}" AND timestamp>="{t}"' + (f" AND {extra}" if extra else "")

    @staticmethod
    def line(e: dict) -> tuple[int, str]:
        t = dt.datetime.fromisoformat(e["timestamp"][:19]).replace(tzinfo=UTC)
        text = e.get("textPayload") or (e.get("jsonPayload") or {}).get("message") or ""
        if not text and e.get("httpRequest"):
            h = e["httpRequest"]
            text = f"HTTP {h.get('requestMethod')} -> {h.get('status')} ({h.get('latency', '?')})"
        return int(t.timestamp() * 1000), f"{e.get('severity', '')} {text}".strip()

    @staticmethod
    def group_key(e: dict) -> str:
        return (e.get("labels") or {}).get("execution_id") or e.get("trace") or e.get("insertId", "-")

    def invocations(self, source: str, start_ms: int, extra: str = "", limit: int = 2000) -> dict:
        hits = self.entries(self.flt(source, start_ms, extra), limit=limit)
        keys = list(dict.fromkeys(self.group_key(e) for e in hits))
        out = {}
        if extra:
            for k in keys[:10]:
                field = "labels.execution_id" if any((e.get("labels") or {}).get("execution_id") == k for e in hits) \
                    else "trace"
                out[k] = sorted(self.line(e) for e in self.entries(self.flt(source, start_ms - 60_000,
                                                                              f'{field}="{k}"'), limit=500))
        else:
            for e in hits:
                out.setdefault(self.group_key(e), []).append(self.line(e))
            out = {k: sorted(v) for k, v in out.items()}
        return out

    def results_24h(self) -> dict:
        hits = self.entries(self.flt("lambda", since_ms("24h"), 'textPayload:"statusCode"'), limit=50_000)
        return result_counts([e.get("textPayload", "") for e in hits])

    def runs(self, job: str, last: int = 5) -> list[dict]:
        reqs = self.entries(self.flt(job, since_ms("30d"), "httpRequest.status>0"), limit=last)
        return [{"id": self.group_key(e), "state": "SUCCEEDED" if e["httpRequest"].get("status") == 200 else
                 f"FAILED (HTTP {e['httpRequest'].get('status')})", "started": e["timestamp"][:19].replace("T", " "),
                 "duration_s": e["httpRequest"].get("latency"), "error": None, "trigger": "HTTP"} for e in reqs]

    def start(self, job: str) -> dict:
        """Call the job's HTTP function with an ID token (the functions accept any authenticated account). The URL
        comes from earlier request logs (e.g. a Cloud Scheduler run); without one: The Cloud Scheduler steps."""
        import requests
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        reqs = self.entries(self.flt(job, since_ms("180d"), "httpRequest.requestUrl:*"), limit=1)
        steps = (f"Cloud Scheduler: Open {self.id}-{GOOGLE_SERVICES[job]}-scheduler (or {self.id}-map-tables-scheduler), "
                 "click RESUME and FORCE RUN, then PAUSE after the run")
        if not reqs:
            return {"started": False, "note": "the function URL is unknown (no earlier HTTP request in the logs) - "
                                              f"start it once in the console: {steps}"}
        url = reqs[0]["httpRequest"]["requestUrl"].split("?")[0]
        tok = service_account.IDTokenCredentials.from_service_account_file(self.key_file, target_audience=url)
        tok.refresh(Request())
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {tok.token}"}, timeout=(15, 20))
            return {"started": True, "url": url, "http_status": r.status_code,
                    "note": "finished within 20 s" if r.ok else r.text[:300]}
        except requests.exceptions.ReadTimeout:
            return {"started": True, "url": url, "note": "running (no response within 20 s, as expected for a long "
                                                         "run) - follow it with: logs --source " + job}


def ops():
    cloud = lake()["cloud"]
    if cloud == "aws":
        return AwsOps()
    if cloud == "google":
        return GoogleOps()
    azure = ("Azure data lakes: Function logs and jobs are not supported here (the profiles hold the Synapse SQL login "
             "and the Flexify S3 keys only). In the Azure portal (resource group of the data lake): Function App "
             "mdftoparquet-<id> > Application Insights (traces | where customDimensions.Category == "
             "\"Host.Function.Console\"); Container App Jobs backlog-<id>, aggregation-<id>, synapse-map-tables-<id> > "
             "Run now / Execution history > Logs. Control files (events.json, backlog.json, ...): control_files.py "
             "with the Flexify S3 profile of the input container")
    if cloud == "azure":
        raise OpsError(azure)
    raise OpsError("This profile is not a data lake (an S3-compatible server): No function logs or jobs. " + azure)


# ---------------------------------------------------------------- commands
def cmd_status(o) -> dict:
    s3 = aws_client("s3")
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=o.info["input_bucket"], Delimiter="/"):
        keys += [x["Key"] for x in page.get("Contents", [])]
    zips = sorted(keys and [k for k in keys if k.startswith("mdf-to-parquet-") and k.endswith(".zip")])
    out = {"lake": o.info["input_bucket"], "cloud": o.info["cloud"], "lake_id": o.id, "function_zips": zips,
           "control_files": [k for k in acf.CONTROL_FILES if k in keys],
           "root_dbcs": [k for k in keys if acf.DBC_FILE_RE.match(k)],
           "passwords_json": "passwords.json" in keys}
    try:
        out["function_24h"] = o.results_24h()
    except OpsError as e:
        out["function_24h"] = {"error": str(e)}
    out["jobs"] = {}
    for job in JOBS:
        try:
            r = o.runs(job, 1)
            out["jobs"][job] = r[0] if r else "no runs"
        except OpsError as e:
            out["jobs"][job] = f"error: {e}"
    return out


def cmd_logs(o, a) -> dict:
    start, states = since_ms(a.since), {}
    if a.source in ("backlog", "aggregation", "map-tables") and isinstance(o, AwsOps):
        runs = o.runs(a.source, 1) if not a.run else [r for r in o.runs(a.source, 50) if r["id"] == a.run]
        if not runs:
            return {"source": a.source, "since": a.since, "groups": {}, "note": f"no {a.source} runs"}
        groups = {r["id"]: o.run_lines(r["id"], since_ms(r["started"]) - 60_000 if r.get("started") else start)
                  for r in runs}
        states = {r["id"]: r for r in runs}
        if a.grep or a.errors:
            groups = {k: [x for x in v if (a.grep and a.grep in x[1]) or (a.errors and re.search(
                r"ERROR|Traceback|CONTROL FILE", x[1]))] for k, v in groups.items()}
    elif isinstance(o, AwsOps):
        pattern = f'"{a.key}"' if a.key else (f'"{a.grep}"' if a.grep else
                                             ('?ERROR ?"CONTROL FILE" ?"Task timed out" ?Traceback' if a.errors else ""))
        groups = o.invocations(start, pattern, limit=a.max * 20)
    else:
        extra = f'textPayload:"{a.key or a.grep}"' if (a.key or a.grep) else ("severity>=ERROR" if a.errors else "")
        groups = o.invocations(a.source, start, extra, limit=a.max * 20)
    out = {}
    missing = sorted(getattr(o, "missing", set()))
    for gid, lines in list(groups.items())[-a.max:]:
        texts = [t for _, t in lines]
        shown = [(t, x) for t, x in lines if x.strip() and (a.all or not NOISE.search(x))]
        res = result_of(texts)
        if gid in states:   # a job run: Its Glue state is the result
            r = states[gid]
            res["result"] = r["state"].lower() + (f": {r['error']}" if r.get("error") else "")
        out[gid] = {"start": iso(lines[0][0]) if lines else None, **res,
                    "lines": [f"{iso(t)} {x[:300]}" for t, x in shown][-a.lines:],
                    **({"hidden_lines": len(lines) - len(shown)} if len(shown) < len(lines) else {})}
    note = f"log group(s) {', '.join(missing)} do not exist yet (no run logged)" if missing and not out else None
    return {"source": a.source, "since": a.since, "groups": out, **({"note": note} if note else {})}


def cmd_diagnose(o, key: str) -> dict:
    from botocore.exceptions import ClientError
    s3, info = aws_client("s3"), o.info
    steps = []
    try:
        h = s3.head_object(Bucket=info["input_bucket"], Key=key)
    except ClientError:
        return {"key": key, "steps": [["object", False, f"not in {info['input_bucket']} - check the key "
                                                         "(device/session/split.MF4, case-sensitive)"]]}
    up = h["LastModified"].astimezone(UTC)
    md = {k.lower(): v for k, v in (h.get("Metadata") or {}).items()}
    steps.append(["object", True, f"{h['ContentLength']:,} bytes, uploaded {up:%Y-%m-%d %H:%M:%S} UTC"
                                  + (f", logging time {md['timestamp']}" if md.get("timestamp") else "")])
    parts = key.split("/")
    trig = key.endswith(TRIGGER_SUFFIXES) if info["cloud"] == "aws" else key.upper().endswith(TRIGGER_SUFFIXES)
    path_ok = len(parts) == 3 and re.fullmatch(r"[0-9A-F]{8}", parts[0])
    steps.append(["trigger", bool(trig and path_ok),
                  "suffix and path trigger the function" if trig and path_ok else
                  ("the suffix must be .MF4/.MFC/.MFE/.MFM in upper case (S3 event filter)" if not trig else
                   "not a <DEVICEID>/<session>/<split> path - the function decodes it but cannot read the device ID")])
    start = int((up - dt.timedelta(minutes=5)).timestamp() * 1000)
    end = int((up + dt.timedelta(hours=2)).timestamp() * 1000)
    if isinstance(o, AwsOps):
        inv = o.invocations(start, f'"{key}"', end_ms=end)
    else:
        inv = o.invocations("lambda", start, f'textPayload:"{key}"')
    if not inv and isinstance(o, AwsOps) and o.lambda_group in o.missing:
        steps.append(["function", False, f"no log group {o.lambda_group} yet: The function has not run (it creates "
                                         "the group at its first run; logs appear ~1 min after a run). Files uploaded "
                                         "before the deployment are only decoded by a backlog run; for new uploads "
                                         "check the Lambda + the bucket's event notification (AWS console)"])
    elif not inv:
        note = ("no function invocation logged for this key - uploaded before the automation was deployed (process "
                "it with a backlog), the bucket trigger is missing, or the logs expired")
        steps.append(["function", False, note])
    else:
        rid, lines = list(inv.items())[-1]
        r = result_of([t for _, t in lines])
        ok = r["result"].startswith("succeeded")
        steps.append(["function", ok, f"{len(inv)} invocation(s), last {iso(lines[0][0])}: {r['result']}"
                                      + (f" (last section: {r['last_section']})" if not ok and r["last_section"] else ""),
                      r["errors"]])
    dev = parts[0]
    session_split = None
    if len(parts) == 3 and re.match(r"^\d{8}", parts[2]):
        session_split = f"{parts[1]}_{parts[2][:8]}"
    try:
        msgs = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=info["output_bucket"], Prefix=f"{dev}/",
                                                                  Delimiter="/"):
            msgs += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
        hits = 0
        if session_split and msgs:
            for m in msgs[:200]:
                for page in s3.get_paginator("list_objects_v2").paginate(Bucket=info["output_bucket"], Prefix=m):
                    if any(o_["Key"].endswith(f"/{session_split}.parquet") for o_ in page.get("Contents", [])):
                        hits += 1
                        break
        steps.append(["parquet", hits > 0, f"{hits} of {len(msgs)} message folder(s) of {dev} have "
                                           f"{session_split}.parquet" if msgs else
                      f"no Parquet folders for {dev} in {info['output_bucket']} (the lake uses the device ID in the "
                      "log file header - the same as the S3 folder for files the device uploaded)"])
    except ClientError as e:
        steps.append(["parquet", False, f"cannot list {info['output_bucket']}: {e.response['Error']['Code']}"])
    try:
        from _backends import connect
        lk = connect(athena=info["cloud"] == "aws", bigquery=info["cloud"] == "google", refresh_schema=True)
        n = sum(1 for t in lk.tables() if t.get("device") == dev)
        has_parquet = steps[-1][0] == "parquet" and "no Parquet folders" not in steps[-1][2]
        steps.append(["catalog", n > 0, f"{n} table(s) for {dev}" if n else
                      f"no tables for {dev}" + (" - run the table mapping job (start --job map-tables): the Parquet "
                                                "files exist" if has_parquet else " (nothing decoded yet)")])
    except Exception as e:  # noqa: BLE001
        steps.append(["catalog", None, f"not checked: {str(e)[:150]}"])
    return {"key": key, "steps": steps}


def cmd_start(o, job: str, wait: bool, timeout: int | None = None) -> dict:
    if timeout and not isinstance(o, AwsOps):
        raise OpsError("--timeout works on AWS only (the Google backlog function has a fixed 60 min timeout): Split the "
                       "backlog into smaller runs instead")
    if timeout and not 1 <= timeout <= 2880:
        raise OpsError("--timeout must be 1-2880 minutes (AWS Glue limit)")
    name = JOBS[job][1]
    if name:
        s3 = aws_client("s3")
        try:
            raw = s3.get_object(Bucket=o.info["input_bucket"], Key=name)["Body"].read()
        except Exception:  # noqa: BLE001
            raise OpsError(f"no {name} in the input bucket root - nothing to process (control_files.py push first)")
        _, issues = acf.parse(name, raw)
        errors = [str(i) for i in issues if i.level == "error"]
        if errors:
            raise OpsError(f"{name} in the bucket is invalid: " + "; ".join(errors[:5]))
    if isinstance(o, AwsOps):
        active = [r for r in o.runs(job, 3) if r["state"] in ("STARTING", "RUNNING", "STOPPING", "WAITING")]
        if active:
            raise OpsError(f"a {job} run is still {active[0]['state']} ({active[0]['id']}) - wait for it")
    res = o.start(job, timeout) if isinstance(o, AwsOps) else o.start(job)
    if wait and isinstance(o, AwsOps):
        fails = 0
        while True:
            try:
                r = next(x for x in o.runs(job, 3) if x["id"] == res["run_id"])
                fails = 0
            except Exception as e:  # noqa: BLE001 - a network blip must not end the wait (the run goes on in AWS)
                fails += 1
                if fails >= 10:
                    raise OpsError(f"lost the connection while waiting ({e.__class__.__name__}) - the run continues: "
                                   f"check it with: runs --job {job}") from e
                time.sleep(30)
                continue
            if r["state"] not in ("STARTING", "RUNNING", "STOPPING", "WAITING"):
                res["final"] = r
                break
            time.sleep(30)
    return res


# ---------------------------------------------------------------- output
def show(cmd: str, res) -> None:
    if cmd == "status":
        print(f"Data lake {res['lake']} ({res['cloud']}, ID {res['lake_id']})")
        print(f"  function zips: {', '.join(res['function_zips']) or 'none'}")
        print(f"  control files: {', '.join(res['control_files']) or 'none'}"
              + (" (+ passwords.json)" if res["passwords_json"] else ""))
        print(f"  root DBCs:     {', '.join(res['root_dbcs']) or 'none'}")
        f = res["function_24h"]
        print("  function, last 24 h: " + (f["error"] if "error" in f else
              f"{f['invocations']} invocation(s)" + (f" {f['results']}" if f["results"] else "")
              + (f" - {f['note']}" if f.get("note") else "")))
        for job, r in res["jobs"].items():
            print(f"  {job:12} " + (r if isinstance(r, str) else
                  f"last run {r['started']}: {r['state']}" + (f" - {r['error']}" if r.get("error") else "")))
    elif cmd == "diagnose":
        print(res["key"])
        for step in res["steps"]:
            mark = {True: "OK  ", False: "FAIL", None: "?   "}[step[1]]
            print(f"  {mark} {step[0]:9} {step[2]}")
            for e in step[3] if len(step) > 3 else []:
                print(f"            {e[:220]}")
    elif cmd == "runs":
        for r in res:
            print(f"  {r['started']}  {r['state']:10} {r.get('duration_s') or '-'} s  {r['id']}"
                  + (f"\n      {r['error']}" if r.get("error") else ""))
        if not res:
            print("  no runs")
    elif cmd == "logs":
        for gid, g in res["groups"].items():
            print(f"== {gid}  {g['start']}  -> {g['result']}"
                  + (f"  ({g['hidden_lines']} routine lines hidden, --all shows them)" if g.get("hidden_lines") else ""))
            for line in g["lines"]:
                print(f"   {line}")
        if not res["groups"]:
            print(f"no log lines ({res['source']}, since {res['since']})" + (f" - {res['note']}" if res.get("note")
                                                                               else ""))
    else:
        print(json.dumps(res, indent=1, default=str))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)   # --env/--json before or after the command
    common.add_argument("--env", default=argparse.SUPPRESS, help="Data lake profile (credentials/<name>.env)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    ap.add_argument("--env", help="Data lake profile (credentials/<name>.env)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common])
    lg = sub.add_parser("logs", parents=[common])
    lg.add_argument("--source", choices=["lambda", *JOBS], default="lambda")
    lg.add_argument("--since", default="24h", help="e.g. 30m, 6h, 7d or an ISO time (UTC)")
    lg.add_argument("--grep")
    lg.add_argument("--key", help="A log file key: the invocations that processed it")
    lg.add_argument("--run", help="Job run ID (default: the last run)")
    lg.add_argument("--errors", action="store_true")
    lg.add_argument("--max", type=int, default=5, help="Invocations/runs to show")
    lg.add_argument("--lines", type=int, default=120, help="Last N lines per invocation/run")
    lg.add_argument("--all", action="store_true", help="Also the routine per-file lines (downloads, uploads)")
    sub.add_parser("diagnose", parents=[common]).add_argument("key")
    r = sub.add_parser("runs", parents=[common])
    r.add_argument("--job", choices=list(JOBS), default="backlog")
    r.add_argument("--last", type=int, default=5)
    s = sub.add_parser("start", parents=[common])
    s.add_argument("--job", choices=list(JOBS), required=True)
    s.add_argument("--wait", action="store_true", help="AWS: Poll every 30 s until the run ends")
    s.add_argument("--timeout", type=int, help="AWS: This run's limit in minutes (default: the job's timeout)")
    a = ap.parse_args()
    use_env(a.env)
    try:
        o = ops()
        res = {"status": lambda: cmd_status(o), "logs": lambda: cmd_logs(o, a), "diagnose": lambda: cmd_diagnose(o, a.key),
               "runs": lambda: o.runs(a.job, a.last), "start": lambda: cmd_start(o, a.job, a.wait, a.timeout)}[a.cmd]()
    except OpsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res, indent=1, default=str)) if a.json else show(a.cmd, res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
