# CANedge AI skills: CAN bus data analysis with AI agents

Open source agent skills for AI CAN bus data analysis with the
[CANedge](https://www.csselectronics.com/pages/can-bus-hardware-products#can-loggers-canedge) CAN bus data logger. Load
them into an AI agent such as [Claude Code](https://www.claude.com/product/claude-code) and it can:

- Configure your devices and review or create DBC files
- Give an instant overview of raw CAN data and explore your S3 bucket
- Decode MF4 log files to a Parquet data lake and query it (local, Amazon, Google Cloud, Azure supported)
- Configure and troubleshoot your data lake automation (email alerts, trip summaries, backlog processing)
- Answer analysis questions as interactive pages (e.g. CAN bus diagnostics)
- Build, edit and import Grafana dashboards
- Build custom tools for your exact needs (e.g. a browser-based Parquet data lake viewer)

The [skills](#the-skills) combine the CANedge Docs, our MF4 tools and Python scripts, and can be adapted to
[other AI agents](#other-ai-agents).

<br>

**Note:** Use the full suite as-is, pick the skills you need, or use it as inspiration for your own skills.

## 1. Set up Claude Code

The below is our recommended setup for new Claude Code users:

1. **Get a [Claude subscription](https://claude.ai)** - Claude Code is included with Claude Pro / Max (or API billing)
2. **Install [Visual Studio Code](https://code.visualstudio.com)**
3. **Install the Claude Code extension** - Extensions panel (`Ctrl+Shift+X`), search **"Claude Code"**, install, sign in
4. **Get the code** - Clone the repo (or download the ZIP from GitHub and extract it)
5. **Open this folder** - *File > Open Folder...* > the cloned repo; skills in `.claude/skills/` load automatically

### Other AI agents

Only the folder layout (`.claude/skills/`, `.claude/settings.json`) is specific to Claude Code: The skills themselves
are Markdown + Python scripts, and `AGENTS.md` holds the project rules. To use another agent (e.g. Codex), open the
repo folder in it and ask it to adapt the repo to its own skill format.


## 2. Set up the skills

### Recommended: Let the agent do it

Open the Claude Code panel in VS Code and ask:

> Set up the CANedge skill suite and check my setup

The agent runs the installer for your OS: It creates a local virtual environment (`.venv`), installs the Python
dependencies and downloads the CSS Electronics MF4 tools. If Python 3.10+ is missing, it offers to install it (via
`winget` on Windows). It finishes with a self-test on the bundled sample data.

### Alternative: Manual setup

1. **Install Python** - [Python 3.10+](https://www.python.org/downloads/); on Windows, tick **"Add python.exe to
   PATH"**. Verify: `python --version`
2. **Run the installer** in the repo folder:
   - **Windows:** double-click **`install.bat`** (or run `install.bat` in a terminal)
   - **Linux / macOS:** `./install.sh` (or `sh install.sh`)


## 3. Optional: Connect your S3 bucket / data lake

Ask the agent to "connect my S3 bucket" (or data lake). It opens a setup window for your keys and bucket name; it
tests them and saves a profile in `credentials/` (gitignored). Never paste keys into the chat. What to enter:

- **AWS data lake:** The 'programmatic access' keys (CloudFormation 'Outputs' tab) + input bucket name
- **AWS S3 bucket only:** The 'device' keys (same 'Outputs' tab or your Configuration File) + bucket name
- **Google Cloud data lake:** The path to your `<id>-programmatic-access-account.json` key file + input bucket name
- **Azure data lake:** The Synapse connection details you also use for Grafana: Serverless SQL endpoint, database,
  the read-only SQL user `sqlreaderuser` + password (deployment output). Your input container is a second profile via your Flexify S3 endpoint (below)
- **MinIO / Flexify / other S3-compatible:** Endpoint URL, keys + bucket (container) name

Connect several buckets once each and tell the agent which one to use. The skills only read your cloud, unless you ask
them to upload, move files, update devices over the air or change the data lake automation; they show the planned
change first and estimate
S3/Athena/BigQuery costs before large jobs. Azure data lakes: Queries, analyses and Grafana dashboards are supported;
the data lake automation tools (function logs, backlog and table mapping jobs) are not.

**Grafana (optional):** Ask the agent to "connect my Grafana". The same kind of window asks for your Grafana URL and a
service account token (Grafana > Administration > Users and access > Service accounts, role Editor). The agent does
not create data sources: Add the Athena / BigQuery / Synapse data source as described in the CANedge Intro. The agent
can then import the dashboards it builds into your Grafana and check that every panel returns data.


## Try it

**Bundled sample:** `sample-data/` holds a minimal dataset from our free EV data pack: 3 log files (2.2 MB, a
39 min Kia EV6 drive: UDS battery data on CAN1, CANmod.gps GNSS/IMU on CAN2) + the 2 DBC files that decode them (see
`sample-data/README.md`). Start with:

> Give me an overview of the raw CAN data in sample-data

> Decode sample-data with the DBCs in sample-data/dbc and show battery current vs speed during the drive

**Your own data:** Point the agent to your log files (SD card copy or any folder), DBC files, Configuration File,
S3 bucket or data lake, for example:

> Use this log file to optimize my Configuration File for size - but keep all battery data at full rate

> Review my DBC files in D:/dbc for errors - and check if they decode sensibly on my data

> Give me a visual overview of my S3 bucket for the last 30 days

> Check all charge terminations in my data lake - does current above the charge limit predict the fault?

> Build a Grafana dashboard for device 3F7A1C2B with the battery signals from this analysis

> Email me when the battery temperature of any device goes above 45 degC

**Note:** Do NOT attach log files or large CSV files to the chat - the agent would try to load millions of CAN frames
into its context window. Reference the file or folder path instead, and let the skills summarize the data.


## The skills

Each skill covers one tool or capability. "Depends on" lists the other skills a skill expects to be present; most
skills also use the shared helper scripts in `canedge-workspace`.
The skills come in two layers: **Tool skills** let an agent use one CANedge tool or API correctly with minimal
wrapping (helper scripts, safety guards, the shared workspace conventions). **Analysis + reporting skills** build
standardized reports and dashboards on top of them - useful for many users, but optional.

| Skill | What it does | Depends on |
|---|---|---|
| **Foundation** | | |
| `canedge-knowledge` | CANedge Docs for AI: Configuration, hardware, log files, protocols | - |
| `canedge-workspace` | Folder layout, projects, DBC-to-device mapping, setup check, helpers | - |
| `canedge-troubleshoot` | Fix devices not recording or uploading, missing data or signals | per symptom |
| `canedge-improve` | Propose a fix to a skill after a session hit a real defect (optional pull request) | - |
| **Tool skills** | | |
| `canedge-mdf2csv` | Convert log files to raw CAN/LIN frames (CSV) | - |
| `canedge-mdf2mdf` | Demux CANmod.router data (CAN11-14, ...), MFC/MFE to MF4 (e.g. for asammdf) | - |
| `canedge-mdf2parquet` | DBC-decode log files to a local Parquet data lake | dbc |
| `canedge-s3` | Explore your S3 bucket (read-only), download log files + configs | - |
| `canedge-s3-migrate` | Archive or move log files between S3 buckets (e.g. to Glacier) | - |
| `canedge-s3-sd-upload` | Upload SD card log files to S3, so they work like device uploads | s3 |
| `canedge-ota` | Over-the-air updates via S3: Firmware, configs (one or many devices), password encryption, bucket moves | config, knowledge |
| `canedge-dbc` | Lint, fix, create (from PDF/Excel), compare and sanity-check DBC files | raw-overview, reports |
| `canedge-config` | Check, edit + simulate Configuration Files (filters, control signals; CANmod.router S1-S4 filters) | mdf2csv, mdf2mdf, reports, knowledge |
| `canedge-lake-query` | Query your Parquet data lake (local, Amazon Athena, Google BigQuery or Azure Synapse) | dbc |
| `canedge-lake-admin` | Configure the data lake automation (events, trip summaries, backlog, routing), read its logs, start jobs | s3, lake-query, dbc |
| `canedge-grafana` | Build, edit and import Grafana dashboards (Athena, BigQuery or Synapse) from your findings | lake-query |
| **Analysis + reporting** | | |
| `canedge-reports` | Shareable reports (HTML + JSON) and the shared dashboard kit | - |
| `canedge-raw-overview` | Interactive overview of raw CAN data across 100s of log files | mdf2csv, reports |
| `canedge-lake-analysis` | Answer analysis questions on your data lake as one interactive page | lake-query, reports |
| `canedge-s3-overview` | Interactive S3 bucket overview: Devices, uploads, devices not logging | s3, reports |


## Folder structure

The skills are designed to reference your raw log files where they are and to write processed data inside this repo
(gitignored). Work that produces something you keep (a report, config, DBC, decoded data) runs in a project: The
agent reuses a matching one or creates one and tells you. Quick questions need no project. When the agent builds a
report or dashboard, it opens it in your default browser and its reply ends with a bold **Open report** link to the
file. We recommend keeping a backup of your log files
and Configuration Files:

```
data/                                   shared caches, reused across projects
  s3/<bucket>/<device>/<session>/...    log files downloaded from S3 (never downloaded twice)
  s3-inventory/<bucket>/                cached S3 object lists + metadata
  sd-upload/                            SD-card upload manifests (canedge-s3-sd-upload)
  athena-schema/, bigquery-schema/,     cached data lake table schemas
  synapse-schema/
  raw-stats/<device>/<session>/...      per-file raw CAN statistics + MF4 header info (meta name, firmware, config)
  lake-admin/<bucket>/                  data lake control files: drafts/, backups/ (before each upload), root DBCs
projects/<yyyy-mm-dd>-<name>/           one folder per analysis (start date)
  project.json / project.md             data sources, devices, timezone, context + findings
  dbc/ (+ dbc-groups.json)              DBC files (+ the DBC-to-device mapping if they differ per device)
  passwords.json                        optional: passwords for encrypted log files
  config/<variant>/config-VV.VV.json    original/ = a copy of your config; one folder per new config (ready to deploy;
                                        several devices: config/<device>/<variant>/)
  parquet/<device>/<message>/...        decoded data (same layout as the S3 data lake)
  data/                                 query results kept for follow-up questions (e.g. fleet summaries)
  grafana/                              Grafana dashboard JSON files for import
  reports/<yyyymmdd>-<name>/            report.html + the agent JSON (report.json, stats.json or results.json)
  temp/                                 scratch scripts + intermediate files, safe to delete
credentials/                            data lake profiles (<name>.env, incl. Azure Synapse logins) + Google key files
                                        + grafana/ (Grafana profiles)
TEMP/                                   drop zone for ad hoc inputs (log files, DBCs, specs) + outputs without a project
                                        (e.g. TEMP/reports/<yyyymmdd>-<name>/)
sample-data/                            the bundled sample log files + DBCs (try it, check_setup.py --selftest)
```

See `.claude/skills/canedge-workspace/references/conventions.md` for the full set of conventions.


## Contributing

We welcome contributions: Open an issue for bugs, questions or ideas, or send a pull request (fixes, new skills,
support for other agents). Please never include confidential data or credentials.

When a session runs into a real problem with a skill (a bug, a wrong result, a misleading instruction), the agent
proposes a minimal, general fix via the `canedge-improve` skill and, if you agree, helps you turn it into a pull
request. Findings specific to your own data or protocol stay in your project notes. The bar for a change is that it
helps most users at a small cost, since every added line, option or script is maintenance for everyone.


## Disclaimer

Use at your own risk. AI agents can make mistakes, so review what the agent proposes before you run or deploy it.
You are responsible for your data (keep backups), your credentials and any cloud costs (S3, Athena, BigQuery, Synapse, data lake jobs) that
the agent's actions cause.


## License and attribution

These skills are fully open source under the [MIT License](LICENSE) - you are free to use, modify and distribute them
in your own projects. Bundled third-party files keep their own license next to them: uPlot + diff2html (MIT), Leaflet (BSD-2) and the
website fonts (OFL) in `canedge-reports/assets/`.

The MF4 converter/decoder executables that the installer downloads (`bin/` folders of `canedge-mdf2csv`,
`canedge-mdf2mdf` and `canedge-mdf2parquet`, not part of the repo) are
proprietary CSS Electronics software, not covered by the MIT License (see [LICENSE](LICENSE)).

