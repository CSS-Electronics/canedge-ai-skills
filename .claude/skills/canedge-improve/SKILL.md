---
name: canedge-improve
description: >
  Propose a fix to a canedge-* skill after a session hit a real, general defect: A script bug or crash, a wrong or
  misleading result, an instruction that sent the agent the wrong way, or a missing check that cost a workaround
  most users would also need. Implement it on the user's OK and offer a pull request so all users benefit. Use at the
  end of such a task, or when the user asks to improve a skill or report a problem with one. Do NOT use after clean
  runs, for ad hoc scripts that combine skills (intended), or for findings about the user's own data, vehicles or
  protocols (those go in project.md).
---

# canedge-improve

The skills should stay small: Frequently used scripts for the common jobs, plus the agent combining them and writing
ad hoc code for the rest. Every added line, option or script is maintenance for everyone, so a change must fix a
real problem at a small cost.

## When to propose (silent otherwise)

Once, at the end of the task, only if the session had one of these:

- A skill script crashed, gave a wrong result, or refused valid input
- A skill's instructions were wrong or misleading, or missed a step every user needs (the agent had to find it out)
- The same workaround would be needed by most users of that skill (not just for this data)

Not a reason: A clean run; an edge case solved by combining skills or a short ad hoc script; the user's protocol,
vehicle, DBC or bucket specifics (-> `project.md`); a personal preference (-> the user's own notes, not the repo).

## The bar (all must hold)

1. **General**: Helps most users of the skill, not only this protocol / vehicle / setup
2. **Real**: Seen in this session (an error, a wrong value, a wasted detour), not hypothetical
3. **Minimal**: The smallest change that fixes it. Prefer fixing or deleting existing lines over new scripts,
   options or sections; one clear line in a SKILL.md beats a paragraph
4. **Clean**: No customer data (device IDs, bucket names, DBC content, file names, values), no credentials

If one fails, say nothing. If the user asked, say why it does not belong upstream and where it fits instead.

## Proposal (one short message, max 3 items)

Per item: **Problem** (what happened, the evidence), **Fix** (files, about how many lines), **Cost** (new options,
lines added, behaviour change). Then ask: Implement it?

## On OK

1. Make the change in the skill files; keep the skill's style and length
2. Re-run the case that failed, plus `check_setup.py --selftest` if a shared script changed
3. Ask whether to share it as a pull request to https://github.com/CSS-Electronics/canedge-ai-skills. If yes:
   Branch `improve/<short-slug>`, commit only the skill change, then with the GitHub CLI `gh repo fork --remote`,
   `git push -u origin HEAD`, `gh pr create` (title = the fix; body = problem, fix, how it was tested, with a
   generic example). Without `gh`: Give the user the PR title + body and the branch to push, or an issue text
4. Never push, open a PR or commit to the user's main branch without the user's OK; check the diff for customer
   data first
