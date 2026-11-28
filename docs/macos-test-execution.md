# Native Qt unit-test execution

The [repository runner](../scripts/run_ci_unittests.py) isolates Qt suites
on macOS and Windows after native failures in single-process discovery and
execution. See [Test selection](development.md#test-selection) for usage.

Full discovery collects exact test IDs and rejects duplicates. On macOS and
Windows, the runner splits the Qt application module and isolates assets,
OCR, Stories and the UI catalogue. Windows full discovery also splits the
remaining tests at a module boundary. Child processes have bounded timeouts.
The runner checks that every selected ID is assigned exactly once and surfaces
assertion failures, signals and non-zero exits.

On macOS and Windows, local selection can collect selected modules directly.
Branch validation checks the full discovery inventory before selecting cases.
Test modules share fixture functions to avoid importing another module's
`TestCase` classes as discovery aliases.

Linux CI runs discovery under Xvfb. Native game capture, audio devices and
release compatibility require their own platform gates.
