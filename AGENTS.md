# Tests

- After code changes, run `uv run python -m scripts.run_changed_unittests --local` first. It selects tests affected by staged, unstaged, and untracked files.
- To validate all changes in the current branch against `origin/main`, run `uv run python -m scripts.run_changed_unittests` without `--local`. Use `--list` to inspect the selection.
- Run the full suite only when the selector requires it, a release gate or user request requires it, or a relevant test is missed by static import analysis. Keep CI's full cross-platform test runs.
