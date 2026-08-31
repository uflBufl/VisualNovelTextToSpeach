# Scoped static typing ratchet

Static typing is introduced as a narrow CI ratchet rather than a repository-wide
conversion. `pyproject.toml` owns the authoritative `mypy` file list and the
macOS quality job runs it with the locked development dependencies.

The initial scope covers one runtime boundary from each high-value category:

- `game_pack.py`: the imported artifact and application-settings boundary;
- `synthesis.py` and `playback.py`: device-independent rendering and playback
  contracts;
- `speech_worker_messages.py`: immutable data crossing the isolated worker
  boundary;
- `authoring/cli_dispatch.py`: command-family orchestration input and result.

`disallow_untyped_defs` prevents new unannotated functions in these modules.
Imports are skipped for this first ratchet so legacy transitive modules are not
silently pulled into scope; this is an explicit limitation, not a claim of
repository-wide type safety. Expansion should add a module to the checked file
list only after its existing errors are fixed. The checked set must never be
reduced merely to make CI pass.
