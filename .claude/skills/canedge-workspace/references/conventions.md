# CANedge workspace conventions

These conventions are shared by all `canedge-*` skills. They keep raw data safe, avoid
re-processing GBs of log files, and make every output traceable and shareable.

## 1. Working directory + environment

- The **working directory is the repo root** (where `.claude/`, `.venv/`, `.env` live). All
  paths below are relative to it. Never `cd` into a skill folder.
- Run every script with the project venv: `.venv\Scripts\python.exe` (Windows) or
  `.venv/bin/python` (macOS/Linux). If it is missing, follow "Setup" below.
- Use only the Python packages in `requirements.txt` (the `.venv`); never install packages (the installer below
  is the exception: It installs `requirements.txt` into `.venv`).
- Scripts live in `.claude/skills/<skill>/scripts/` and are invoked by their full relative path,
  e.g. `.venv\Scripts\python.exe .claude/skills/canedge-workspace/scripts/check_setup.py`.
- CSS Electronics tools: `.claude/skills/canedge-mdf2csv/bin/mdf2csv[.exe]`,
  `.claude/skills/canedge-mdf2mdf/bin/mdf2mdf[.exe]` (CANmod.router demux, MFC/MFE to MF4) and
  `.claude/skills/canedge-mdf2parquet/bin/mdf2parquet_decode[.exe]`, downloaded (not in git) by `install.bat` /
  `install.sh` for Windows x64, Linux x64 and Linux ARM64. Missing or outdated (`check_setup.py` says so): Run
  `canedge-workspace/scripts/install_tools.py`. No macOS build: Linux VM/container, or `--tool <path>` /
  `CANEDGE_MDF2CSV` / `CANEDGE_MDF2MDF` / `CANEDGE_MDF2PARQUET` for a build elsewhere.

### Setup (first run): Offer it, then do it

Signals: A session-start note `CANEDGE SETUP NEEDED` (from `.claude/hooks/setup_check.sh`; it names the OS and
what is missing), `.venv/` missing, or a script failing on a missing package or tool. Then, before other work:

1. **Offer it in one yes/no question** (AskUserQuestion where the client has it), with the OS-specific command and
   what it does, e.g. "Set everything up now? I'll run `install.bat`: Creates `.venv`, installs the Python packages
   and downloads the CSS MF4 converter + decoder (2-5 min)". If the user asked something else, say setup comes first
   and that you continue with their request afterwards. Only the tools missing: Offer `install_tools.py` instead
2. **On yes, run it** from the repo root (timeout 10 min): Windows `cmd //c '.\install.bat'` (Bash tool; the `.\` is
   required) or `.\install.bat` (PowerShell); Linux/macOS `sh install.sh`
3. **No Python 3.10+** (the installer says so): Windows: Offer
   `winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements`,
   then re-run `install.bat` (it finds the new Python without a terminal restart). Linux: The user runs
   `sudo apt install python3 python3-venv` (or their distro's equivalent) themselves, as it needs their password;
   never run `sudo`. macOS: `brew install python` if Homebrew exists, else python.org
4. **Verify**: `check_setup.py --selftest`; report READY in one line, or only the failing item and its fix
5. **Then the data**: Unless the request already says, ask where the data is: Log files on disk (folder path) or an
   S3 bucket / data lake (setup window, section 7). Then continue with the user's original request

macOS: The installer sets up `.venv` but skips the MF4 tools (no build). Say so once, only if a task needs them.

## 2. Data safety (hard rules)

1. **Raw log files are never moved, renamed, modified or deleted** - wherever they live (SD card,
   network drive, `data/s3/`). Never pass `-d/--delete-converted` to the CSS tools.
2. Never copy raw data into project folders. Projects **reference** raw data by path.
3. Derived data (stats, Parquet, CSV) is regenerable, but **ask before deleting it** - it may have
   taken a long time to produce.
4. **Size guard**: Before converting/decoding more than ~1 GB of log files (or downloading more
   than ~1 GB from S3), tell the user the file count, total size and a rough time estimate (S3: plus the
   egress cost), and ask before proceeding (offer a sample, e.g. one split per session, as an alternative).
   One confirmed size covers the whole job (`fetch_logs.py --max-gb N`). For "what is wrong / which device / root
   cause" questions, recommend the full data set: A sample or hand-picked files can miss the cause. Queries on an
   existing local Parquet lake need no size guard.
5. Never print secrets (keys) - not in chat, not in reports, not in logs.

## 3. Folder layout

**When to use a project.** Any task that produces something the user keeps (a report or dashboard, a config, a DBC,
decoded Parquet, query results, Grafana JSON) runs in a project:

- Reuse a matching project first: Same devices or bucket and the same question or topic (`new_project.py --list`).
  Otherwise create one with `new_project.py` without asking, and name it in the reply ("Created project
  `2026-11-05-charging-fault`"). Ask only when two existing projects could both fit
- Quick looks that only answer in chat (listing files, `check_setup`, a knowledge question, a one-off CSV of a single
  file) need no project. Their scratch output goes to `TEMP/`
- Scripts that write a page without `--project` fall back to `TEMP/reports/<yyyymmdd>-<slug>/`. That is a safety net,
  not the normal path

**Fixed vs flexible.** Scripts and other skills rely on the left column; the right column is the agent's choice:

| Fixed | Agent's choice |
|---|---|
| Project name `<yyyy-mm-dd>-<slug>`, `project.json` + `project.md` | The slug, and whether to reuse a project or start a new one |
| `dbc/` (+ `dbc-groups.json`), `parquet/`, `config/`, `grafana/`, `data/` | Report slugs, config variant names, how an analysis splits into several reports |
| Pages in `reports/<yyyymmdd>-<slug>/` with `report.html` | Extra files in a report folder (`analysis.py`, `custom.js`, intermediate data) |
| Shared caches in the root `data/`, never per project | Extra project subfolders when a task needs them: Note each in `project.md` "Context + caveats" so later sessions find it |
| Raw data referenced by path, never copied | What goes in `temp/` (scratch, safe to delete) |

```
<repo>/
├── .claude/skills/...                 # the skill suite
├── .venv/  requirements.txt  install.bat
├── credentials/                       # data lake profiles <name>.env + BigQuery keys + grafana/<name>.env (gitignored, section 7)
├── TEMP/                              # quick looks + script fallback without a project, e.g. TEMP/reports/<yyyymmdd>-<slug>/ (gitignored)
├── data/                              # SHARED caches, reused by all projects (gitignored)
│   ├── s3/<bucket>/<device>/<session>/<split>.MF4      # S3 downloads, mirrors the S3 keys
│   ├── s3-inventory/<bucket>/<device>.json             # cached S3 object list + metadata
│   ├── raw-stats/<device>/<session>/<split>.json       # per-file raw CAN stats (mdf2csv)
│   ├── athena-schema/  bigquery-schema/  synapse-schema/   # cached data lake table schemas (canedge-lake-query)
│   ├── sd-upload/                                       # canedge-s3-sd-upload manifests
│   ├── lake-admin/<bucket>/                             # canedge-lake-admin: drafts/, backups/, root/dbc/
│   └── firmware/<device type>/firmware-VV.VV.VV.zip     # official firmware packages (canedge-ota, MD5 checked)
└── projects/<yyyy-mm-dd>-<slug>/      # one folder per analysis project, prefixed with its start date (gitignored)
    ├── project.json                   # machine-readable: sources, timezone, DBC mapping
    ├── project.md                     # human notes: goal, context, caveats, findings log
    ├── dbc/                           # project DBC files (or copies of the user's DBCs)
    ├── dbc-groups.json                # optional: DBC-to-device mapping if the DBCs differ per device (section 5)
    ├── passwords.json                 # optional: passwords for encrypted log files (never printed)
    ├── config/<variant>/config-VV.VV.json  # original/ = copy of the user's config; one folder per new config; several devices: config/<device>/<variant>/ (on demand)
    ├── parquet/<device>/<CANx_Message>/<yyyy>/<mm>/<dd>/<session>_<split>.parquet
    ├── data/                          # query results kept for follow-ups (e.g. lake-query summary .parquet) (on demand)
    ├── grafana/                       # dashboard specs + JSON for import (canedge-grafana) (on demand)
    ├── ota/                           # canedge-ota: <utc>-<label>/ plans (plan.md, files to upload, before-configs, apply log) + user-filled secrets.csv (on demand)
    ├── temp/                          # scratch scripts + intermediate files, safe to delete
    └── reports/<yyyymmdd>-<slug>/     # one folder per page: report.html + the agent JSON (section 6)
```

Why shared vs per project: S3 downloads and raw stats do not depend on DBC files, so they are
cached once (keyed by device/session/split) and reused. Decoded Parquet depends on the DBC
mapping, so it lives in the project. The local Parquet layout is identical to the CANedge S3
data lake layout, so analysis code ports to Athena with minimal changes.

## 4. project.json

```json
{
  "name": "2026-01-15-charging-fault",
  "created": "2026-01-15",
  "description": "Why does vehicle X not drive after charging to 100%?",
  "sources": [
    {"type": "local", "path": "D:/LOG"},
    {"type": "s3", "bucket": "my-bucket", "prefix": "12345678/",
     "start_utc": "2026-01-15T00:00:00Z", "end_utc": "2026-01-16T00:00:00Z"}
  ],
  "devices": ["12345678"],
  "display_tz": "Europe/Berlin",
  "notes": "Free text - anything the agent must remember about this data"
}
```

- `sources`: Local folders/files (read in place) and S3 prefixes (downloaded once to `data/s3/`
  via the canedge-s3 skill, then read from there). Extending the data = adding a source.
- `devices`: Optional filter.
- `display_tz` (optional): All data, stats and queries stay in **UTC**; reports and chat show local
  time. Default: The PC's time zone. Set an IANA name (e.g. `Europe/Berlin`) when the
  user wants another zone, e.g. where the vehicles operate. IANA names follow daylight saving
  time; the legacy fixed `display_utc_offset` (e.g. `+02:00`) is still read but does not.
  Helper: `canedge-reports/scripts/display_time.py`. Note: Some users configure the device RTC
  to local time; if so, record it in `notes` and correct for it explicitly.
- Create a project with `scripts/new_project.py`.

## 5. DBC-to-device mapping (dbc-groups.json)

**Never assume that all DBC files apply to all devices unless confirmed.** A DBC applied to the
wrong device can produce plausible-looking but wrong values (e.g. two vehicle types that use the
same CAN ID for different messages). In most setups all DBCs do apply to all devices, so one
question is usually enough.

- **No `dbc-groups.json`** in the project (the default): All DBCs in `dbc/` apply to all devices,
  the same rule as the data lake automation. Use this when the user confirms it (or the source of
  truth below says so)
- **`dbc-groups.json`**: Only when the DBCs differ per device. Each device then gets only the DBCs
  of its group; a device in no group is refused by the decoding scripts

The format is identical to the CANedge data lake automation's `dbc-groups.json`:

```json
{
  "dbc_groups": [
    {"devices": ["12345678"], "dbc_files": ["can1-12345678.dbc", "can2-12345678.dbc"]},
    {"devices": ["AABBCCDD", "11223344"], "dbc_files": ["can1-fleet.dbc"]}
  ]
}
```

- `dbc_files` are file names inside the project's `dbc/` folder.
- Optional `"common_dbc_files": [...]` (top level, automation v4.4.0+): DBCs added to every group's `dbc_files`, e.g.
  the J1939-73 DM1 DBCs that all devices use, so they are not repeated per group.
- The **channel** is taken from the file name prefix: `can1-` -> CAN1, `can2-` -> CAN2,
  `can9-` -> CAN9 (internal GNSS/IMU), `can11-`, `can12-`, ... -> the CANmod.router channels after demux (section 5b),
  `lin1-`/`lin2-` -> LIN. A file without a prefix applies to CAN1 and CAN2 (ask the user to rename it if that is not
  intended).

Resolve it before the first step that matches IDs to DBCs (config filters, DBC coverage, decoding), not after
it: Ask, end the turn and wait (no work on an assumed mapping meanwhile). Copy all the user's DBCs into the
project, also when the task is about one device: Picking that device's DBCs by file name is assuming the
mapping. How to resolve the mapping (in this order):

| Situation | Source of truth |
|---|---|
| An earlier project in this workspace has a confirmed mapping for these devices + DBC files | Reuse it (copy `dbc-groups.json` + the DBCs it names), do not ask again |
| S3 data lake with `dbc-groups.json` in the input bucket root | Use that file as-is (download it into the project) |
| S3 data lake with only `canX-*.dbc` files in the bucket root | All root DBCs apply to all devices (that is what the automation does): No `dbc-groups.json` |
| No data lake (CANedge1, simple CANedge2/CANedge3 setups) | **Ask the user** once: "Do these DBCs apply to all devices?" If not, propose a mapping from file names (e.g. `can1-12345678.dbc`, `dbc_groups.py propose`) or a README and confirm it. Evidence for the question: `dbc_groups.py check --dbc <all DBCs>` lists CAN IDs the DBCs define differently |

Record the answer in `project.md` (Data section). If the DBCs differ per device, store the mapping
as `projects/<name>/dbc-groups.json`. Either way, run `scripts/dbc_groups.py check --project
projects/<name>` (files exist, channels, devices, duplicate CAN IDs / J1939 PGNs within a DBC - the
decoder refuses that DBC - and overlaps between DBCs on the same channel; without `dbc-groups.json`
this checks all DBCs in `dbc/` together). Across DBCs, the same ID/PGN is only allowed with different
message names (separate Parquet folders, e.g. the J1939-73 DM1 workaround); the same message name with
different signals gives corrupt/mixed Parquet although decoding "works" (error).

## 5b. CANmod.router (CANedge + router)

A CANmod.router on the CANedge CAN2 sends its 4 channels (S1-S4) as CAN FD **tunnel frames** on one ID (default
0x10; a 2nd daisy-chained router e.g. 0x12). Raw data then shows CAN2 with one big FD ID 0x010 instead of the router
traffic. **Demux** turns the tunnel frames back into the original frames: 0x10 -> CAN11-14 (S1-S4), 0x12 -> CAN15-18
(CAN11+ never clashes with CAN9). After demux CAN2 only holds other CAN2 traffic (e.g. other CANmods).

- **Detect**: `raw_stats.py` (raw overview, `dbc_coverage.py`) and `csv_summary.py` flag "looks like CANmod.router
  data that is not demuxed" (CAN2 0x010/0x012 CAN FD). Ask the user to confirm the router (and how many, which
  tunnel IDs if changed) before going on
- **Switch it on per project**: Copy the router DBC `canedge-mdf2mdf/assets/can2-canmod-router.dbc` into the
  project's `dbc/` (in the `dbc-groups.json` group of the router devices if there is one). Its MUX-TP attribute
  (`TransportProtocolType "MUXTP#11:12:13:14"` on 0x10, `"MUXTP#15:16:17:18"` on 0x12) is the **single source of truth**: The MF4 decoders demux
  with it, and every canedge-* script demuxes the device's files with the same mapping (`muxtp.py`, mdf2mdf on the
  fly, temporary files deleted): Raw overview, `csv_summary.py --project`, config simulation, DBC coverage. Raw
  stats are cached per mapping (`data/raw-stats` recomputes when it changes)
- **Other tunnel IDs** (not 0x10/0x12): `muxtp.py dbc --id 010:11 --id 014:15 -o <p>/dbc/can2-canmod-router.dbc`
  (replaces the default router DBC; the IDs come from each router config, `phy.can.route.mux.s2p.message.id`)
- **DBCs for the router channels**: Prefix `can11-` (S1) ... `can14-` (S4), `can15-` ... for a 2nd router. Ask the
  user which DBC is on which S channel (users often still have `can1-`/`can2-` names from a direct connection)
- **Data lake automation**: Router DBC + `can11-` DBCs in the input bucket root work; a `dbc-groups.json` listing
  `can11-*.dbc` needs automation v4.3.2+ (canedge-lake-admin)
- **Configs**: The router has its own Configuration File (S1-S4 filters, separate router report in canedge-config);
  the CANedge config only has to log the tunnel ID unprescaled at the router's bit-rate (1M/4M FD by default)
- `muxtp.py show --project <p>` prints the mapping and the mdf2mdf flags; a demuxed MF4 copy (asammdf, Vector
  tools): `canedge-mdf2mdf`

## 6. Outputs

- Every analysis output that a human will read is a report built with the **canedge-reports**
  skill: Self-contained HTML + JSON with the underlying numbers.
- Every report states the data used (section "Data used"): Device IDs, sessions/splits (or S3
  keys / SQL), time range (local time + UTC), DBC files per device, tool versions, generated time.
- Scripts that produce data for the agent write compact JSON (aggregated facts, not raw frames).
  Never load raw CSV/Parquet row data into the conversation - summarize with scripts.
- Page builders print `OPEN <label>: file:///...`. The agent copies that URL into its reply as an **Open report**
  link and opens the turn's final page in the browser with `scripts/paths.py open <file>` (canedge-workspace
  "Replies to the user"); path helpers: `scripts/paths.py` (`report_dir()`, `open_line()`)

**Where outputs go** (the "Without a project" column is for quick looks and the script fallback, section 3):

| Output | In a project | Without a project |
|---|---|---|
| Page: Report, dashboard, analysis page, config or DBC diff | `reports/<yyyymmdd>-<slug>/` | `TEMP/reports/<yyyymmdd>-<slug>/` |
| Query results kept for follow-ups | `data/` | `TEMP/data/` |
| Decoded Parquet | `parquet/` | `TEMP/lake/` |
| Configs | `config/<variant>/`, several devices: `config/<device>/<variant>/` | `TEMP/config/<variant>/` |
| Grafana dashboard JSON | `grafana/` | `TEMP/grafana/` |
| Scratch scripts + intermediate files | `temp/` | `TEMP/` |

- Report slug: kebab-case `<topic>[-<device>][-<variant>]`, e.g. `raw-overview`, `s3-overview-<bucket>`,
  `config-<variant>`, `dbc-changes`, `p0a08-root-cause`. `<yyyymmdd>` = the local run date
- Page files: Always `report.html`. The agent JSON per page type: `report.json` (static reports via build_report,
  S3 overview, config impact page), `stats.json` (raw overview), `results.json` (analysis page; also its
  input). DBC diffs are HTML only. Questions about a page: Answer from its JSON (the same data the user sees), never by
  opening the page in a browser tool
- A new report folder in a project gets one line in the `project.md` findings log (date, folder, key result)

## 7. Credentials: One profile per bucket / data lake

A **profile** (`credentials/<name>.env`, gitignored, named after the input bucket) is one bucket or data lake: The
input bucket, its output bucket (`<input>-parquet`) and the lake's query service. It is created with
`setup_env.py`; the variables are cloud-neutral:

- `CANEDGE_CLOUD` (`aws`, `google`, `s3` = any S3-compatible server such as MinIO or Flexify, `azure` = the Synapse
  SQL login of an Azure data lake), `CANEDGE_ACCESS_KEY_ID`,
  `CANEDGE_SECRET_ACCESS_KEY`, `CANEDGE_INPUT_BUCKET` (entered); `CANEDGE_REGION`, `CANEDGE_LAKE_ID` (derived, AWS);
  `CANEDGE_S3_ENDPOINT` (Google device keys: `https://storage.googleapis.com`, `s3`: the server);
  `CANEDGE_GOOGLE_KEY_FILE` (Google data lake: a service account key for the buckets via `gcs_s3.py` and BigQuery),
  `CANEDGE_KEY_TYPE` (derived: what the keys allow, table below), optional `CANEDGE_BIGQUERY_DATASET` / `_PROJECT`;
  Azure: `CANEDGE_SYNAPSE_ENDPOINT`, `CANEDGE_SYNAPSE_DATABASE`, `CANEDGE_SYNAPSE_USER`, `CANEDGE_SYNAPSE_PASSWORD`
- `env.py` `lake()` derives the rest: Output bucket `<input>-parquet`, Athena database `dbparquet<id>` and workgroup
  `athenaworkgroup-<id>` (id = the stack's UniqueID). Never ask the user for these
- There is no root `.env`: Keys and settings live only in the profiles (an old root `.env` is ignored)

What the keys allow (the setup detects AWS key types from the keys' IAM user and says so):

| Keys | S3 skills | Data lake queries |
|---|---|---|
| AWS: The data lake stack's programmatic access keys | Input + output bucket | Athena (+ function/job logs, Glue job runs: `canedge-lake-admin`) |
| AWS: The device keys (only an S3 input bucket, no data lake: about half of the AWS users) | Input bucket | - |
| AWS: The Athena user's keys | - | Athena |
| Google: The data lake's programmatic access key file (`<id>-programmatic-access-account.json`) | Input + output bucket | BigQuery (+ function logs: `canedge-lake-admin`) |
| Google: The device keys (S3 interoperability keys; only an input bucket, no data lake) | Input bucket | - |
| Google: The BigQuery user's key file (`<id>-bigquery-user-account.json`, Grafana's) | Read only | BigQuery |
| S3-compatible server (e.g. MinIO): Endpoint + keys | Bucket | - |
| Azure: The Synapse read-only login `sqlreaderuser` (serverless SQL endpoint, database, password; Grafana's) | - | Synapse (no lake admin) |
| Azure: The Synapse SQL admin `sqladminuser` (older deployments without the read-only login) | - | Synapse (the skills still only read) |
| Azure: The Flexify endpoint + keys of the input container (an S3-compatible profile) | Input container | - |

The Athena / BigQuery user's keys (Grafana's) are the query-only option, e.g. for a team member who should not get
the data lake owner's programmatic access keys. A restricted profile is normal: When a task needs more than the keys
allow, the script stops with a message saying which keys it needs. Pass that on; never suggest the full keys for
query work.

**No profile yet** (or a script says so): Ask which cloud if unclear (most users: AWS), then:

- **AWS / S3-compatible:** Open the setup window with `.venv\Scripts\python.exe .claude/skills/canedge-workspace/scripts/setup_env.py` (`--cloud s3` for
  MinIO etc.; `.venv/bin/python` on macOS/Linux). First tell the user that a small window opens where they paste the
  keys and the bucket name from the stack's 'Outputs' tab in AWS CloudFormation (data lake deployed: the
  'programmatic access' keys; only a bucket: the 'device' keys; query access only: the 'Athena' keys) and click
  'Test and save'
- **Google data lake:** Ask for the path of the programmatic access key file `<id>-programmatic-access-account.json`
  (the deployment saved it in the input bucket root; the user downloads it; query access only: the BigQuery user's
  key file `<id>-bigquery-user-account.json`) and the input bucket name - neither is secret. Then run `setup_env.py --key-file "<path>" --bucket <name>` (no window)
- **Google, only an input bucket:** `setup_env.py --cloud google` (window with the device keys + bucket name)
- **Azure data lake:** `setup_env.py --cloud azure` (window: Serverless SQL endpoint, database, SQL user, password -
  the values Grafana uses, shown at the end of the Synapse deployment; database + password also in the Container App
  Job `synapse-map-tables-<id>`: env `SYNAPSE_DATABASE`, secret `synapse-reader-password`). Profile name = the
  workspace (`synapse-<id>`). Tell the user to use the read-only login `sqlreaderuser` (empty user field); a
  deployment from before 2026-11 has none until Synapse is deployed again and the mapping job has run: Then
  `sqladminuser` + the secret `synapse-password` works too (the skills only run SELECT statements either way). The input container
  (raw log files, control files) is a second profile: `setup_env.py --cloud s3` with the Flexify endpoint URL + keys
  and the container name. Function logs and jobs (backlog, aggregation, table mapping) are not supported on Azure

The call returns the result without secrets: Profile, key type, buckets, region, Athena names or BigQuery datasets
and OK/FAIL per check. Report it (incl. what the keys allow), and give the user the Athena database/workgroup if
they need them (e.g. for Grafana). Without Tk (rare), the user runs the script with `--cli` in a terminal.
`check_setup.py --cloud` re-runs the same tests.

Keys that allow Athena/BigQuery say nothing about the lake's content: Log files uploaded before the deployment are
only in the lake once the user processes the backlog (an empty output bucket = nothing decoded yet). Before
suggesting queries, check with `canedge-lake-query` `lake_schema.py` (Glue catalog, free) which devices have tables.
Until then, don't comment on the lake's content.

**Several buckets / data lakes:** One profile each (named after its input bucket). Every S3/Athena/BigQuery script
takes `--env <name>`; with one profile it is used automatically, with several the scripts stop and list them. A
project notes its profile in its S3 source (`"env": "<name>"`, printed by `fetch_logs.py`): Pass the same `--env`
for that project. The output bucket of a lake is never a separate profile.

**Grafana** (canedge-grafana `grafana_api.py`: Import and check dashboards in the user's Grafana): Not a data lake, so
its profile is `credentials/grafana/<name>.env` (`GRAFANA_URL`, `GRAFANA_TOKEN` + the detected organization, version
and role; named after the host, e.g. `myorg-grafana-net`; several: `--grafana-env <name>`). Create it with
`setup_env.py --grafana` (window: Grafana URL + service account token). First tell the user how to get the token:
Grafana > Administration > Users and access > Service accounts > Add service account with the role **Editor** (imports
and edits dashboards and folders, runs their queries: Enough to import a dashboard and check its panels) > Add service
account token. The skills never create or change data sources (the user adds them as the CANedge Intro
describes); permissions per data source need Grafana Enterprise/Cloud. The result says which role the token has and whether a
data source named `Amazon Athena` / `Google BigQuery` / `Microsoft SQL Server` (Synapse) exists. `check_setup.py --grafana` re-runs the tests. The token
follows the rules for keys below.

Keys must never enter the agent's context (the chat is sent to the model provider and kept in the session
transcript): Never ask for keys in the chat and never open a profile or key file with a read tool. Keys pasted into
the chat anyway: Do not repeat them, open the setup window instead, and suggest rotating them if that matters.

## 8. CANedge log file basics

- Folder structure: `LOG/<device ID>/<session>/<split>.MF4` (SD card) and
  `<device ID>/<session>/<split>.MF4` (S3). Device ID = 8 hex chars, session/split = 8 digits.
  S3 file names may carry a suffix, e.g. `00000003-6501A2B3.MF4` (added on upload; ignore it).
- Extensions: `.MF4` (plain), `.MFC` (compressed), `.MFE` (encrypted), `.MFM` (both). The CSS
  tools read all of them. Encrypted files need a password file: All scripts use `--passwords <path>`,
  else `projects/<name>/passwords.json` (with `--project`), and refuse MFE/MFM files without one up
  front (`tools.password_file` / `passwords_refusal`). Never read, print or ask for the passwords.
- The MF4 header (first 64 KB) holds device ID, session, split, firmware, config CRC32, meta name and
  the CAN/LIN bit-rates. `scripts/mf4_files.py` reads these without decoding the file (the one header
  reader of the suite). It reads .MF4 files only: For MFC/MFE/MFM the header fields are unknown, and
  device/session/split come from the folder path.
- MFC/MFE/MFM header: `mf4_files.fill_headers()` reads it from a finalized copy (mdf2mdf, deleted at
  once) of ONE file per device/session and applies it to all splits of the session (header
  `finalized`; firmware, config and meta only change with a restart = new session). Raw stats (incl.
  bus load), local decoding (device meta name) and the config simulation (recorded config CRC32) do
  this; plain `mf4_files.py` listings do not (cost ~0.6 s per MB of the finalized file). mdf2mdf
  24.12.19 drops the LIN bit-rates from the header.
- The header start time can be the **session** start rather than the split start; use the first
  frame timestamp for exact split time spans.
- CANedge2/CANedge3 uploads to S3 carry object metadata incl. the logging time (`Timestamp`).
  Always filter by logging time, not by S3 upload time (`LastModified`). The `Timestamp` is the exact
  UTC file start only from FW 01.09.01; older firmware wrote approximate values (local time and/or the
  file end, see canedge-s3 `references/s3-details.md`).
