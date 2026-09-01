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
signal or non-zero child exit fails the parent command. The qt-app child has a
60-second timeout and the remainder child a 300-second timeout, so native
teardown hangs fail the runner instead of blocking CI indefinitely. Targeted unittest
arguments and non-macOS execution keep the ordinary single-process behavior.
The README, CI workflows and macOS package build use this same runner.

Test modules reuse ordinary fixture functions, so imported `TestCase` objects
cannot become discovery aliases. A repository regression
asserts that raw `unittest` discovery contains no duplicate IDs; the runner does
not deduplicate or hide violations.

The Chatterbox stop-interruption regression uses an Event-based audio fake; it
does not share `unittest.mock` state across the playback and assertion threads.
On 2026-09-01 two consecutive acceptance runs each executed 102 app tests and
1,851 remaining tests: 1,953 unique exact IDs in total, with zero discovery
aliases. Both runs completed successfully without ignoring a native exit code.
