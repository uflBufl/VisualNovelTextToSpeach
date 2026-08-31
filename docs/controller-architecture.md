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

`tests/test_controller_components.py` is the architectural gate. It verifies
the composition, public delegation and the one-expression facade rule. New
application operations must be assigned to the appropriate component instead
of adding coordination logic to a public `AppController` method. Private
implementation methods may call other private implementation methods, but may
not re-enter any public component-backed facade operation; an AST gate enforces
that one-way dependency. A later implementation extraction can move the
remaining private stateful algorithms behind the same component protocol
without another UI-facing API migration.
