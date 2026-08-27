# macOS unit-test execution

Use the repository runner for the canonical full suite:

```bash
uv run --frozen python scripts/run_ci_unittests.py discover -s tests
```

On macOS, importing the complete PySide6 test inventory and then executing
`tests.test_app` in that same process reproducibly caused a native `SIGSEGV`.
The app module passes on its own, and every remaining test passes together in a
separate process. The supported runner therefore discovers the suite once,
canonicalizes duplicate exact IDs exposed when fixture `TestCase` classes are
imported by other test modules, and runs two exact inventories:

1. every `tests.test_app.*` test;
2. every other exact discovered test.

The partition rejects overlap or incomplete coverage, each child checks its
executed count against its inventory, and any exception, assertion failure,
signal or non-zero child exit fails the parent command. Targeted unittest
arguments and non-macOS execution keep the ordinary single-process behavior.
The macOS package build uses this same runner.

The 2026-08-27 acceptance run executed 74 app tests and 1,312 remaining tests:
1,386 unique exact IDs in total. It collapsed 87 legacy discovery aliases and
completed both shards successfully without ignoring a native exit code.
