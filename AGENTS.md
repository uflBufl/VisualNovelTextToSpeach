# Fixes

- Find and fix the root cause instead of masking a failure with monkey patches
  or scenario-specific workarounds.
- Check related scenarios for the same cause and fix all affected paths, not
  only the scenario that exposed the bug.
- When replacing an implementation, migrate its consumers and remove the
  superseded code in the same change. If a migration period is necessary, record
  in [todo.md](todo.md) what legacy remains, why it is still needed and the
  concrete condition for removing it.

# Checks

- Run tests related to the change and affected consumers. Broader fast,
  inexpensive checks are fine; expensive checks and full-suite runs need a
  reason tied to affected behavior, a specific risk or a required gate.
- After code changes, run `uv run python -m scripts.run_changed_unittests --local`
  first and add related checks missed by its static analysis. See
  [Test selection](README.md#test-selection) for branch validation and commands.
