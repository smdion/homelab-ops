# TODO — Intake

> This file is **intake only**. Items added here are processed by Claude and published to
> the [Tasks & Roadmap](https://docs.seandion.com/tasks/) section of the docs site.
>
> Tier-based tracking lives at: `homelab-docs/docs/tasks/index.md`

## Processing Rules

When the user says "process the TODO list", follow these steps **in order**:

1. **Intake** — Move every item from "Pending User Added" into the appropriate tier section
   on the homelab-docs site (`docs/tasks/index.md`).
   - Clear the pending list when done (leave the empty `- ` placeholder).
2. **Research** — Cross-reference each new item against the homelab-docs site (`/workspace/homelab-docs`),
   and the codebase for context (related playbooks, vars, known constraints).
3. **Rate** — Assign `Risk · Effort · Impact` (H/M/L) to each new item.
4. **Place** — Sort the item into the correct tier (1–5) on the docs site.
   If a matching section already exists, add it there; otherwise create a new section heading.
5. **Merge** — If the new item overlaps with an existing task, combine them.
6. **Format** — Ensure every actionable item uses `- [ ]` checkbox format.
7. **Publish** — Commit and push homelab-docs, then run Semaphore template 114.
8. **Cleanup prompt** — After processing, list any completed (`[x]`) tasks and ask the user
   whether to remove them.

---

## Pending User Added

- Lets complelty recreate README from scratch, we have an authoritave DOCS website. The readme shoudl give the user an idea how to use the project.

- backup offline is back online (blocked todo) the unraid side still has individual shares, but I want to mount only one share (with subfolders of hte unraid shares) on the synology side. Help me re-work the playbook and run in dry mode until we are sure it works1

- a lot of tasks fail silently, we need to review/fix this

- stack_definitions should drive compose stack creation — containers declare their stack in container_definitions, compose files should be generated from definitions (definitions are source of truth, not compose files)