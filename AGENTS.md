# Project agent instructions

## Planning and TODO discipline

- Before implementing any non-trivial plan with multiple steps, decisions,
  dependencies, risks, or acceptance gates, first record the complete current
  plan in `todo.md`.
- Keep every still-relevant planned step in `todo.md`, including work that is
  deferred, blocked on the user, or intentionally sequenced after another
  change. Update the plan before implementing newly discovered work so that no
  agreed item exists only in chat context.
- Do not begin implementation of a multi-point plan until its actionable steps
  and completion gates are present in `todo.md`. A single isolated and obvious
  fix does not require a new planning section.
- When verified work is complete, follow the repository TODO policy: move
  durable behavior, evidence, and procedures to `docs/`, then remove the
  completed TODO entries and any empty section.
