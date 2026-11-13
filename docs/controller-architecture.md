# Application controller boundaries

`AppController` remains the stable application-facing facade and composition
root. UI adapters, replay and tests may continue calling its established public
methods, but those methods delegate to one of four explicit coordination
components:

- `RuntimeLifecycleComponent` owns startup, settings replacement and shutdown.
  Its request-scoped settings guard serializes runtime apply, defines the atomic
  commit point and releases only the matching live-reader wait on cancellation;
- `LiveSessionComponent` owns live-reading controls and auto-advance state;
- `VoiceAssignmentComponent` owns assignment, preview and narrator-fallback
  operations;
- `DiagnosticsComponent` owns capture inspection and pipeline diagnostics.

The components are created once by `AppController.__init__`. Each retains its
own typed private port: lifecycle cannot see voice or diagnostics operations,
live-session cannot reach lifecycle state, and diagnostics cannot mutate voice
assignment. `controller_components.py` is part of the non-shrinkable mypy scope.
This keeps the existing cancellation, routing, callback and thread-safety
behavior intact while separating the application entry points by responsibility.
Stateful implementation methods are private and are not a supported caller
surface. `DiagnosticsComponent` owns capture geometry, the latest snapshot,
pipeline metrics and the explicit inspect/test flows directly; these are no
longer proxy callbacks on `AppController`. It receives only the capture, OCR,
voice and reporting collaborators described by its narrow protocol.
`LiveSessionComponent` also owns the one-shot read, playback queue controls,
story-scope identification, session preflight/start/stop, emergency stop and
auto-advance setting. Its capture
helper lives in `live_snapshot.py`, so the component does not depend circularly
on the compatibility facade. Low-level sequence publication, backend live-mode
switching and speaker-corpus revalidation remain injected collaborators.
`VoiceAssignmentComponent` owns voice inventory, assignment mutations,
narrator-fallback staging, scoped chapter/corpus preflight discovery and voice
preview lifecycle. Backend preview, narrator application and cache invalidation
remain injected low-level collaborators rather than parallel public operations.
`RuntimeLifecycleComponent` owns startup, runtime settings reconfiguration, its
exact cancellation/commit boundary, ordered shutdown and executor cleanup.
Startup cancellation is terminal until an explicit `prepare_startup()` begins a
new user-requested start. Quit sets that cancellation before UI task invalidation
and applies the live reader's persistent emergency-stop guard before executor
shutdown, so late OCR or synthesis completions cannot enqueue playback.
`AppController` injects backend, executor, reader, registry/router and scheduler
factories as the composition root; lifecycle public operations no longer proxy
back into controller implementations.

New application operations belong to the component responsible for their
behavior. `tests/test_controller_components.py` exercises playback outcomes,
cancellation, shutdown and settings propagation; the tests follow the
[behavior-focused testing principles](testing-principles.md). Static analysis
checks collaborator types. Internal class layout, annotation spelling and the
number of statements in facade methods can change while preserving those
behaviors and the established application-facing API.
