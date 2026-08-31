# Application controller boundaries

`AppController` remains the stable application-facing facade and composition
root. UI adapters, replay and tests may continue calling its established public
methods, but those methods delegate to one of four explicit coordination
components:

- `RuntimeLifecycleComponent` owns startup, settings replacement and shutdown;
- `LiveSessionComponent` owns live-reading controls and auto-advance state;
- `VoiceAssignmentComponent` owns assignment, preview and narrator-fallback
  operations;
- `DiagnosticsComponent` owns capture inspection and pipeline diagnostics.

The components are created once by `AppController.__init__` and retain a typed
private implementation protocol back to the controller state. This keeps the
existing cancellation, routing, callback and thread-safety behavior intact
while separating the application entry points by responsibility. Stateful
implementation methods are private and are not a supported caller surface.

`tests/test_controller_components.py` is the architectural gate. It verifies
the composition, public delegation and the one-expression facade rule. New
application operations must be assigned to the appropriate component instead
of adding coordination logic to a public `AppController` method. A later
implementation extraction can move private stateful algorithms behind the same
component protocol without another UI-facing API migration.
