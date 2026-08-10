# Agent Instructions

Before running Android or iOS device automation, read `CLAUDE.md` and
`AUTOMATION_PERMISSIONS.md` completely.

For a device run, present the resolved ExecutionPlan and request one authorization covering only the allowlisted
workflow. After authorization, continue through setup, capture, validation, report generation, GitHub Pages publish,
and opening the public report without repeated conversational approval for allowlisted steps. Never bypass mandatory
host-product security prompts, and request fresh authorization for anything outside the allowlist.
