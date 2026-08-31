# Repository guidance

- Treat `skills/` as the only editable source of Skill content.
- Keep every Skill self-contained. Do not introduce runtime imports from sibling Skill directories.
- Record discovery, dependencies, portability, and provenance in `catalog/skills.json`.
- Put harness-specific packaging under `adapters/<harness>/`; generated files belong in `dist/` and must not be committed.
- Preserve each Skill's existing invocation policy unless a change is explicitly requested.
- Run `./tooling/validate.sh` after changing a Skill, catalog entry, bundle, adapter, or packaging tool.
- Do not commit, push, publish, or install globally unless the requested end state includes that action.
