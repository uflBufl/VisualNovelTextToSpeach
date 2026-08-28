# Desktop accessibility invariants

Qt form rows that contain more than one control must expose the same semantics
as an ordinary `QFormLayout` field. Create an explicit label, bind it to the
primary input with `QLabel.setBuddy()`, and give the input and adjacent action
buttons non-empty accessible names and descriptions. This applies in particular
to path fields paired with Browse buttons and editable selectors paired with a
Refresh button. Tests introspect both the buddy relationship and the accessible
metadata so a layout refactor cannot silently remove screen-reader context.

Non-modal `QMessageBox` decisions on macOS must keep an explicit strong
reference to the active prompt through the next event-loop turn. Qt closes a
message box as part of its native button handling and may still call
`QDialogButtonBox::standardButton()` after the Python `clicked` slot returns.
Decision handlers therefore disable the prompt immediately, retain it, and
defer the application action; clearing the final reference synchronously can
produce a native use-after-free rather than a catchable Python exception.

## Auto-advance focus authority

Auto advance posts a system-level key, so inability to prove that the selected
game owns keyboard focus is a hard stop. Focus-probe exceptions are interpreted
as `not focused`; they are never permission to dispatch input. A ready dialogue
remains pending and is dispatched at most once after focus is proven again.

On macOS, `CGWindowListCopyWindowInfo` Z-order is not keyboard-focus authority.
A fullscreen game on another display or Space can remain first in that list
while a different application receives keyboard input. The selected window's
owner PID is therefore compared with `NSWorkspace.frontmostApplication()` at
capture time and again immediately before dispatch.

VNTTS deliberately does not activate the game automatically. Stealing focus
while the user is typing in another application is disruptive and can make an
otherwise safe pending action surprising. A future focus-restoration option
must be explicit and disabled by default; after requesting activation it must
recheck the active PID before posting the single advance key.
