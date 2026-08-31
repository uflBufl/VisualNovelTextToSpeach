# Maintainability ratchets

`scripts/check_maintainability.py` inventories production Python sources and is
run by the CI quality job. It rejects three kinds of new debt:

- imports of underscore-prefixed names from another `vntts` module, including
  calls through an explicit module alias such as `import vntts.owner as owner`;
- modules or functions that newly exceed the configured line ceiling;
- functions that newly exceed the lightweight AST branch-complexity ceiling.

The versioned baseline is
`tests/fixtures/maintainability-baseline-v1.json`. Its private-import entries are
exact source/target/name exceptions. Oversized modules and functions have an
individual ceiling equal to the measured debt at baseline creation. Existing
code may not grow past that ceiling. When it shrinks, CI reports the old ceiling
as stale until the baseline is lowered to the new exact measurement; when it
crosses below a threshold, the obsolete entry must be removed. A new offender
is rejected even when it is smaller than the repository's largest legacy
offender. Complexity counts ordinary `try` and Python 3.11 `try`/`except*`
branches consistently.

The current thresholds are 2000 module lines, 200 function lines and complexity
50. They are guardrails for exceptional debt, not preferred design targets.
Normal additions should remain substantially smaller.

Run the gate with:

```bash
uv run --frozen python scripts/check_maintainability.py
```

`--inventory` prints a baseline-shaped current inventory without modifying the
repository. Baseline updates require review: add an exception only for an
intentional compatibility constraint, never merely to turn CI green. When debt
is reduced or removed, its baseline ceiling must be lowered or deleted in the
same change; the checker enforces this freshness rule.

The 2026-08-31 self-service pregeneration and sequence-first integration kept
the established application, controller, generation-state and backend facades
compatible while moving the new pipeline implementations into focused modules.
Its reviewed integration growth is recorded as exact per-symbol ceilings in the
baseline; the global thresholds and private-import inventory were not relaxed.
These ceilings describe remaining debt, not preferred sizes, and any further
growth still fails CI.
