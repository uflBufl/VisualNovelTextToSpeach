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
