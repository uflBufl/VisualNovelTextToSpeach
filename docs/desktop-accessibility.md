# Desktop accessibility invariants

Qt form rows that contain more than one control must expose the same semantics
as an ordinary `QFormLayout` field. Create an explicit label, bind it to the
primary input with `QLabel.setBuddy()`, and give the input and adjacent action
buttons non-empty accessible names and descriptions. This applies in particular
to path fields paired with Browse buttons and editable selectors paired with a
Refresh button. Tests introspect both the buddy relationship and the accessible
metadata so a layout refactor cannot silently remove screen-reader context.
