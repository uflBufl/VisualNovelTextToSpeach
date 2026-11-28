# Application controller boundaries

**Status: current implementation reference.** The public facade is in
`vntts/controller.py`; coordination components are in
`vntts/controller_components.py`.

`AppController` is the application-facing facade and composition root. UI
adapters, replay and tests use its public methods; the facade delegates the
following responsibilities:

| Component | Responsibility |
| --- | --- |
| `RuntimeLifecycleComponent` | Startup, runtime settings replacement, ordered shutdown and executor cleanup |
| `LiveSessionComponent` | One-shot reading, story-scope identification, live start/stop, transport, emergency stop and auto-advance settings |
| `VoiceAssignmentComponent` | Voice inventory, assignment, narrator-fallback decisions, scoped preflight discovery and preview lifecycle |
| `DiagnosticsComponent` | Capture geometry, snapshots, pipeline metrics and explicit inspect/test flows |

Each component currently retains an `AppController` reference and accesses its
shared state and collaborators. This is a responsibility split, not an enforced
capability boundary: the implementation does not give each component an
isolated private port. Backend protocols type specific collaborators, and mypy
checks the application modules through the `vntts` scope in `pyproject.toml`.
Private helpers and component layout are not an extension API.

The lifecycle component's request-scoped settings guard serializes runtime
apply, defines the commit point and releases only the matching live-reader
wait on cancellation. Startup cancellation remains terminal until an explicit
`prepare_startup()`. Quit sets cancellation before invalidating UI tasks and
applies the reader's persistent emergency-stop guard before executor shutdown,
so late OCR or synthesis cannot restart playback.

`AppController` supplies backend, executor, reader, registry/router and scheduler
factories. Shared sequence publication, backend live-mode switching and corpus
revalidation remain controller collaborators. One-shot capture helpers live in
`live_snapshot.py`; diagnostics and voice components coordinate their flows
without creating another public controller API.

New operations belong with their responsible component. Relevant behavior is
covered by `tests/test_controller_components.py` and affected controller/live
consumer tests. Preserve cancellation, callback ownership, routing, settings
propagation and shutdown outcomes; follow the
[testing principles](testing-principles.md), rather than asserting facade size
or an internal class arrangement.
