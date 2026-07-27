# skills-for-fabric submission — `tableau-migration` skill

This folder stages a contribution to
[`microsoft/skills-for-fabric`](https://github.com/microsoft/skills-for-fabric). It is **not** part of
the accelerator's runtime — it is the guidance-only skill that teaches an AI agent how to run a
Tableau → Power BI / Fabric migration, modeled on the existing `databricks-migration` skill.

## Layout (drop into a fork as-is)

```
skills/
  tableau-migration/
    SKILL.md                       # the skill (frontmatter + body)
    resources/
      construct-mapping.md         # detailed construct-by-construct map
```

The paths under `contrib/skills-for-fabric/` here mirror exactly where these files go in a fork of
`microsoft/skills-for-fabric` (i.e. copy `contrib/skills-for-fabric/skills/tableau-migration/` to
`skills/tableau-migration/` in the fork).

## How to submit (recommended order)

1. **Open an issue first** on `microsoft/skills-for-fabric` proposing a `tableau-migration` skill and
   confirm a maintainer will accept it. The public repo is a downstream mirror of an internal Microsoft
   repo (every release commit is *"Release vX.Y.Z from internal repo"*), so buy-in before a large PR
   matters.
2. **Fork** the repo, create a branch, and copy `skills/tableau-migration/` into it.
3. Run whatever checks the repo defines (see its `.github/workflows/` and `AGENTS.md`).
4. **Open the PR**; sign the Microsoft CLA when the bot prompts.
5. Keep the skill guidance-only — link the accelerator repo as the reference implementation; do not
   vendor the Python engine into `skills-for-fabric`.

## Notes

- The skill is self-contained instructions (like `databricks-migration`), not code.
- It references two sibling skills in that repo (`semantic-model-authoring`,
  `powerbi-report-authoring`) and `common/COMMON-CORE.md`; those links resolve inside the fork, not here.
