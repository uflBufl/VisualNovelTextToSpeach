# macOS unit-test execution

Use the repository runner for the canonical full suite:

```bash
uv run --frozen python scripts/run_ci_unittests.py discover -s tests
```

On macOS, importing the complete PySide6 test inventory and then executing
`tests.test_app` in that same process reproducibly caused a native `SIGSEGV`.
The app module passes on its own, and every remaining test passes together in a
separate process. The supported runner therefore discovers the suite once,
rejects duplicate exact IDs, and runs two exact inventories:

1. every `tests.test_app.*` test;
2. every other exact discovered test.

The partition rejects overlap or incomplete coverage, each child checks its
executed count against its inventory, and any exception, assertion failure,
signal or non-zero child exit fails the parent command. Targeted unittest
arguments and non-macOS execution keep the ordinary single-process behavior.
The README, CI workflows and macOS package build use this same runner.

Test modules reuse ordinary fixture functions, so imported `TestCase` objects
cannot become discovery aliases. A repository regression
asserts that raw `unittest` discovery contains no duplicate IDs; the runner does
not deduplicate or hide violations.

The 2026-08-28 acceptance run executed 74 app tests and 1,385 remaining tests:
1,459 unique exact IDs in total, with zero discovery aliases. Both shards
completed successfully without ignoring a native exit code.
