# CANedge AI data analysis workspace

This folder is a workspace for analyzing CANedge CAN/LIN data with the skills in `.claude/skills/`.

- Setup missing (a `CANEDGE SETUP NEEDED` session note, no `.venv/`, or a missing package/tool): Before other work,
  offer to set everything up for the user's OS as one yes/no question and run the installer on yes (details:
  canedge-workspace conventions section 1 "Setup")
- Start any data task with the `canedge-workspace` skill and follow its conventions
  (`.claude/skills/canedge-workspace/references/conventions.md`)
- When continuing work in an existing `projects/<date>-<slug>/` folder, read its `project.md` (notes, caveats,
  findings from earlier sessions) and `project.json` first
- Keep replies short: What was done, the result, the next step. State only facts a script or the data showed, never
  assumed ones. No em/en dashes; capitalize the first word after a colon. When a page (report, dashboard) was built,
  end with its link in bold on its own line, e.g. **[Open report](file:///...)**, copied from the script's `OPEN`
  line, and open the final page in the browser with canedge-workspace `paths.py open <file>` (details:
  canedge-workspace "Replies to the user")
- Work that produces something the user keeps runs in a project: Reuse a matching one, else create one (details:
  canedge-workspace conventions section 3)
- Run all scripts with the project venv from this folder: `.venv\Scripts\python.exe` (Windows) or `.venv/bin/python`
- Ask for 1-3 key context facts (vehicle, protocol, symptom) when they would change the approach, rather than guess
  (details: canedge-workspace "Asking for context")
- Never move, modify or delete the user's raw log files; never load raw frames into the conversation
- Never assume all DBC files apply to all devices - ask the user (usually they do; if not, use a `dbc-groups.json`)
- Credentials live in data lake profiles `credentials/<name>.env` (Grafana: `credentials/grafana/<name>.env`), created
  with the setup window (canedge-workspace `setup_env.py`, `--grafana`) - never print them, read them or ask the user to
  paste them into the chat
- Share results as reports via the `canedge-reports` skill (self-contained HTML + JSON with data provenance)
- If a skill had a real, general defect in the session (bug, wrong result, misleading instruction), propose a minimal
  fix at the end of the task via the `canedge-improve` skill; stay silent after clean runs
