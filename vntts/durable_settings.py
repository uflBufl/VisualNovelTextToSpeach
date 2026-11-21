"""Failure-atomic settings mutations shared by the desktop application shell."""

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Protocol, TypeVar

from PySide6.QtWidgets import QDialog

from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.auto_advance_policy import (
    auto_advance_control_state,
    guard_auto_advance_settings,
)
from vntts.ocr import (
    CAPTURE_COORDINATE_SPACE,
    DialogRegion,
    default_dialog_region,
    get_dialog_region_file,
    save_dialog_region,
    save_story_region,
)
from vntts.profiles import GameProfileStore, profiles_schema_version
from vntts.settings import (
    AppSettings,
    capture_target_identity,
    load_app_settings,
    preference_values,
    settings_schema_version,
    user_preferences,
)
from vntts.versioned_json import (
    StaleDocumentError,
    file_revision,
    read_versioned_json_snapshot,
    write_json_if_unchanged,
    write_versioned_json_if_unchanged,
)

if TYPE_CHECKING:
    from vntts.configuration_apply import _Controller, _Dashboard, _Signals


type CaptureAreaEdits = dict[
    tuple[str, str | None], tuple[DialogRegion, DialogRegion | None]
]


class _OnboardingTestPage(Protocol):
    def set_result(self, succeeded: bool, message: str) -> None: ...

    def set_progress(self, percent: int | None, message: str) -> None: ...


_MISSING = object()
_WriteResult = TypeVar("_WriteResult")


def _capture_snapshot(
    path: Path, *, profile_id: str | None = None
) -> tuple[dict[str, object], bytes | None]:
    try:
        return read_versioned_json_snapshot(
            path,
            schema_version=profiles_schema_version
            if profile_id is not None
            else settings_schema_version,
            document_name="Game profiles"
            if profile_id is not None
            else "Setup configuration",
            allow_older=True,
            allow_unversioned=True,
        )
    except FileNotFoundError:
        return {}, None


def _area_history_path() -> Path:
    path = get_dialog_region_file()
    return path.with_name(f"{path.stem}.targets.json")


def _target_key(mode: str, title: str | None) -> str:
    _, name = capture_target_identity(mode, title)
    if mode == "screen":
        return "screen"
    return f"window:{name}" if name else ""


def remembered_capture_areas(
    current: AppSettings,
    dialogue: DialogRegion | None,
    story: DialogRegion | None,
) -> Callable[[str, str], tuple[DialogRegion | None, DialogRegion | None]]:
    try:
        history, _ = _capture_snapshot(_area_history_path())
    except OSError, ValueError:
        history = {}
    current_key = _target_key(current.capture_mode, current.game_window_title)

    def lookup(
        mode: str, title: str
    ) -> tuple[DialogRegion | None, DialogRegion | None]:
        key = _target_key(mode, title)
        if key == current_key:
            return dialogue, story
        entry = history.get(key)
        if not isinstance(entry, dict):
            return None, None
        try:
            area = DialogRegion.from_mapping(entry)
            wide = entry.get("story_region")
            return area, DialogRegion.from_mapping(wide) if wide else None
        except ValueError:
            return None, None

    return lookup


def _capture_fields(
    document: dict[str, object], profile_id: str | None
) -> dict[str, object]:
    if profile_id is None:
        return document
    profiles = document.get("profiles", [])
    if isinstance(profiles, list):
        for profile in profiles:
            if isinstance(profile, dict) and profile.get("id") == profile_id:
                return profile
    return {}


@dataclass
class _CaptureRollback:
    path: Path
    profile_id: str | None
    before: dict[str, object]
    expected: dict[str, object]
    missing_before: bool = False

    def restore(self) -> None:
        document, revision = _capture_snapshot(self.path, profile_id=self.profile_id)
        current = _capture_fields(document, self.profile_id)
        if any(
            current.get(key, _MISSING) != value for key, value in self.expected.items()
        ):
            return
        for key in self.expected:
            previous = self.before.get(key, _MISSING)
            if previous is _MISSING:
                current.pop(key, None)
            else:
                current[key] = previous
        if not document and self.missing_before:
            lock_path = self.path.with_name(f"{self.path.name}.lock")
            with exclusive_advisory_lock(lock_path, blocking=True):
                if file_revision(self.path) != revision:
                    raise StaleDocumentError(
                        "Setup configuration changed during rollback"
                    )
                self.path.unlink(missing_ok=True)
            return
        write_json_if_unchanged(
            self.path, document, revision=revision, document_name="Setup configuration"
        )


def _capture_write(
    path: Path,
    profile_id: str | None,
    expected: Mapping[str, object],
    write: Callable[[], _WriteResult],
    rollback: list[_CaptureRollback] | None,
    *,
    snapshot: tuple[dict[str, object], bytes | None] | None = None,
) -> _WriteResult:
    if rollback is None:
        return write()
    before, revision = snapshot or _capture_snapshot(path, profile_id=profile_id)
    previous = _capture_fields(before, profile_id).copy()
    changed = {
        key: value
        for key, value in expected.items()
        if previous.get(key, _MISSING) != value
    }
    token = (
        _CaptureRollback(path, profile_id, previous, changed, revision is None)
        if changed
        else None
    )
    if token is not None:
        rollback.append(token)
    try:
        return write()
    except StaleDocumentError as error:
        if token is not None and not error.publication_started:
            rollback.remove(token)
        raise


def _replace_capture_document(
    path: Path,
    snapshot: tuple[dict[str, object], bytes | None],
    updated: dict[str, object],
    rollback: list[_CaptureRollback],
) -> None:
    before, revision = snapshot
    if before == updated:
        return
    _capture_write(
        path,
        None,
        {k: updated.get(k, _MISSING) for k in before.keys() | updated.keys()},
        lambda: write_json_if_unchanged(
            path, updated, revision=revision, document_name="Capture areas"
        ),
        rollback,
        snapshot=snapshot,
    )


class _OnboardingWizard(Protocol):
    start_live_after_save: bool

    @property
    def test_page(self) -> _OnboardingTestPage: ...

    def settings(self) -> AppSettings: ...

    def capture_regions(self) -> tuple[DialogRegion | None, DialogRegion | None]: ...

    def deleteLater(self) -> None: ...

    def show(self) -> None: ...

    def raise_(self) -> None: ...

    def activateWindow(self) -> None: ...


class DurableSettingsMixin:
    """Persist settings candidates before publishing them to runtime state."""

    if TYPE_CHECKING:
        settings: AppSettings
        controller: _Controller
        dashboard: _Dashboard
        _controller_busy: bool
        profile_store: GameProfileStore
        signals: _Signals
        onboarding_wizard: _OnboardingWizard | None
        onboarding_cancel_event: Event
        _onboarding_preview_settings: AppSettings | None
        _onboarding_reading_settings: AppSettings | None
        _onboarding_start_live: bool
        _reading_window_retry_settings: AppSettings | None
        _reading_window_hint: str | None
        _shutting_down: bool
        _settings_path: Path
        _settings_revision: bytes | None
        _settings_commit_lock: Lock
        _last_saved_settings: AppSettings | None

        def _update_auto_advance_action(self) -> None: ...

        def _refresh_preparation_settings(self) -> None: ...

        def _apply_controller_action_state(self) -> None: ...

        def _onboarding_runtime_settings(
            self, settings: AppSettings
        ) -> AppSettings: ...

        def _retain_preparation_reading(self, settings: AppSettings) -> None: ...

        def _bind_preparation_callbacks(self) -> None: ...

        def prepare_reading(
            self,
            *,
            reading_settings: AppSettings | None = None,
            start_live: bool = False,
        ) -> None: ...

        def set_status(self, message: str | None) -> None: ...

        def _set_reading_status(self, message: str) -> None: ...

        def set_ready(self, ready: bool) -> None: ...

        def show_error(self, message: str) -> None: ...

        def show_dashboard(self) -> None: ...

        def _start_configuration_apply(
            self, settings: AppSettings, *, progress_status: str, success_status: str
        ) -> None: ...

    def _save_settings_candidate(
        self,
        candidate: AppSettings,
        *,
        dialog_region: DialogRegion | None = None,
        story_region: DialogRegion | None = None,
        area_edits: CaptureAreaEdits | None = None,
    ) -> Path:
        with self._settings_commit_lock:
            values = asdict(candidate)
            if persisted := self._last_saved_settings:
                current = asdict(self.settings)
                # Unchanged runtime overrides must not replace saved values.
                for name, saved_value in asdict(persisted).items():
                    if current[name] != saved_value and values[name] == current[name]:
                        values[name] = saved_value
            values = preference_values(values)
            saved_settings = AppSettings.from_mapping(values)
            previous = self._last_saved_settings or self.settings
            try:
                settings_before, settings_revision = _capture_snapshot(
                    self._settings_path
                )
            except ValueError as error:
                raise StaleDocumentError(
                    "Settings changed on disk or are unsupported; reopen before saving"
                ) from error
            if settings_revision != self._settings_revision:
                raise StaleDocumentError(
                    "Settings changed on disk; reopen before saving"
                )
            rollback: list[_CaptureRollback] = []
            try:
                target = capture_target_identity(
                    saved_settings.capture_mode, saved_settings.game_window_title
                )
                old_target = capture_target_identity(
                    previous.capture_mode, previous.game_window_title
                )
                edits = dict(area_edits or {})
                edited_active = edits.pop(target, None)
                if edited_active is not None:
                    dialog_region, story_region = edited_active
                if old_target != target:
                    if previous.active_profile_id and old_target in edits:
                        self._save_setup_capture_regions(
                            previous,
                            *edits[old_target],
                            rollback=rollback,
                            replace_story=True,
                        )
                    completed = self._switch_capture_areas(
                        previous,
                        saved_settings,
                        rollback,
                        restore=dialog_region is None
                        and saved_settings.active_profile_id is None,
                    )
                    if completed:
                        saved_settings = saved_settings.updated(
                            onboarding_completed=True
                        )
                        values["onboarding_completed"] = True
                if edits:
                    history_path = _area_history_path()
                    snapshot = _capture_snapshot(history_path)
                    history = dict(snapshot[0])
                    for (mode, title), (dialogue, wide) in edits.items():
                        key = _target_key(mode, title)
                        prior = history.get(key)
                        entry = {
                            **dialogue.to_json(),
                            "coordinate_space": CAPTURE_COORDINATE_SPACE,
                            "completed": mode == "screen"
                            or isinstance(prior, dict)
                            and prior.get("completed") is True,
                        }
                        if wide is not None:
                            entry["story_region"] = wide.to_json()
                        history[key] = entry
                    _replace_capture_document(history_path, snapshot, history, rollback)
                if dialog_region is not None:
                    self._save_setup_capture_regions(
                        candidate,
                        dialog_region,
                        story_region,
                        rollback=rollback,
                        replace_story=edited_active is not None,
                    )
                self._settings_revision = _capture_write(
                    self._settings_path,
                    None,
                    values,
                    lambda: write_versioned_json_if_unchanged(
                        self._settings_path,
                        settings_schema_version,
                        values,
                        revision=self._settings_revision,
                        document_name="Settings",
                    ),
                    rollback,
                )
            except OSError, ValueError:
                self._rollback_setup_writes(rollback, settings_before)
                raise
            self._last_saved_settings = saved_settings
            return self._settings_path

    def _switch_capture_areas(
        self,
        previous: AppSettings,
        selected: AppSettings,
        rollback: list[_CaptureRollback],
        *,
        restore: bool,
    ) -> bool:
        # OCR reads only the active area file. Load saved targets when switching sources.
        path = get_dialog_region_file()
        active_snapshot = _capture_snapshot(path)
        active, _ = active_snapshot
        history_path = _area_history_path()
        history_snapshot = _capture_snapshot(history_path)
        history, _ = history_snapshot
        key = _target_key(previous.capture_mode, previous.game_window_title)
        incoming = (
            history.get(_target_key(selected.capture_mode, selected.game_window_title))
            if restore
            else None
        )
        fields = (
            *default_dialog_region.to_json(),
            "coordinate_space",
            "ocr_regions",
            "story_region",
        )
        outgoing: dict[str, object] = {}
        if previous.active_profile_id is None:
            outgoing = {name: active[name] for name in fields if name in active}
            if active.get("story_binding") != {
                "capture_mode": previous.capture_mode,
                "game_window_title": previous.game_window_title,
            }:
                outgoing.pop("story_region", None)
        else:
            profile = self.profile_store.get(previous.active_profile_id)
            if profile is not None and key == _target_key(
                profile.capture_mode, profile.game_window_title
            ):
                outgoing = {
                    **profile.dialog_region.to_json(),
                    "coordinate_space": profile.coordinate_space,
                }
                if profile.story_region is not None:
                    outgoing["story_region"] = profile.story_region.to_json()
                if profile.ocr_regions is not None:
                    outgoing["ocr_regions"] = profile.ocr_regions.to_json()
        if (
            key
            and "left" in outgoing
            and (
                previous.onboarding_completed
                or outgoing != default_dialog_region.to_json()
            )
            and outgoing.get("coordinate_space") in (None, CAPTURE_COORDINATE_SPACE)
        ):
            outgoing["completed"] = previous.onboarding_completed
            _replace_capture_document(
                history_path, history_snapshot, {**history, key: outgoing}, rollback
            )

        restored: dict[str, object] = dict(default_dialog_region.to_json())
        if isinstance(incoming, dict):
            restored.update({k: incoming[k] for k in fields if k in incoming})
            if "story_region" in restored:
                restored["story_binding"] = {
                    "capture_mode": selected.capture_mode,
                    "game_window_title": selected.game_window_title,
                }
        if active or isinstance(incoming, dict):
            updated = {
                k: v for k, v in active.items() if k not in (*fields, "story_binding")
            }
            updated.update(restored)
            _replace_capture_document(path, active_snapshot, updated, rollback)
        return isinstance(incoming, dict) and incoming.get("completed") is True

    def _rollback_setup_writes(
        self, rollback: list[_CaptureRollback], settings_before: dict[str, object]
    ) -> None:
        failures = []
        for capture in reversed(rollback):
            try:
                capture.restore()
            except (OSError, ValueError) as error:
                failures.append(str(error))
        if any(capture.profile_id is not None for capture in rollback):
            refreshed = GameProfileStore.load(self.profile_store.path)
            self.profile_store.profiles = refreshed.profiles
            self.profile_store._revision = refreshed._revision
        current, revision = _capture_snapshot(self._settings_path)
        if current == settings_before:
            self._settings_revision = revision
        if failures:
            raise OSError("Setup rollback failed: " + "; ".join(failures))

    def _save_setup_capture_regions(
        self,
        settings: AppSettings,
        dialog_region: DialogRegion,
        story_region: DialogRegion | None,
        *,
        rollback: list[_CaptureRollback] | None = None,
        replace_story: bool = False,
    ) -> None:
        if story_region is not None and not story_region.contains(dialog_region):
            raise ValueError("The story area must include the entire dialogue region")
        profile_id = settings.active_profile_id
        if profile_id is not None:
            profile = self.profile_store.get(profile_id)
            if profile is None or (profile.capture_mode, profile.game_window_title) != (
                settings.capture_mode,
                settings.game_window_title,
            ):
                raise ValueError(
                    "The game profile changed. Select the reading areas again"
                )
            _capture_write(
                self.profile_store.path,
                profile_id,
                {
                    "dialog_region": dialog_region.to_json(),
                    "ocr_regions": None,
                    "coordinate_space": CAPTURE_COORDINATE_SPACE,
                    "story_region": None
                    if profile.needs_recalibration
                    else (
                        profile.story_region.to_json()
                        if profile.story_region is not None
                        else None
                    ),
                },
                lambda: self.profile_store.update_region(profile_id, dialog_region),
                rollback,
            )
            if story_region is not None or replace_story:
                _capture_write(
                    self.profile_store.path,
                    profile_id,
                    {"story_region": story_region.to_json() if story_region else None},
                    lambda: self.profile_store.update_story_region(
                        profile_id, story_region
                    ),
                    rollback,
                )
        else:
            path = get_dialog_region_file()
            _capture_write(
                path,
                None,
                {
                    **dialog_region.to_json(),
                    "coordinate_space": CAPTURE_COORDINATE_SPACE,
                    "ocr_regions": _MISSING,
                },
                lambda: save_dialog_region(dialog_region, path),
                rollback,
            )
            if story_region is not None or replace_story:
                _capture_write(
                    path,
                    None,
                    {
                        "story_region": story_region.to_json()
                        if story_region
                        else _MISSING,
                        "story_binding": {
                            "capture_mode": settings.capture_mode,
                            "game_window_title": settings.game_window_title,
                        }
                        if story_region
                        else _MISSING,
                    },
                    lambda: save_story_region(
                        story_region,
                        path,
                        capture_mode=settings.capture_mode,
                        game_window_title=settings.game_window_title,
                    ),
                    rollback,
                )

    def toggle_auto_advance(self, enabled: bool) -> None:
        if self._controller_busy:
            self._update_auto_advance_action()
            self.set_status("Reading controls are updating. Try again when ready.")
            return
        allowed, effective, reason = auto_advance_control_state(
            self.settings.capture_mode,
            self.settings.live_sequence_mode,
            enabled,
        )
        if not allowed:
            self._update_auto_advance_action()
            self.set_status(reason)
            return
        candidate = guard_auto_advance_settings(
            self.settings.updated(auto_advance_enabled=effective)
        )
        try:
            self._save_settings_candidate(candidate)
        except OSError as error:
            self._update_auto_advance_action()
            self.show_error(f"Unable to save auto-advance setting: {error}")
            return
        self.settings = candidate
        self._update_auto_advance_action()
        self.controller.set_auto_advance_enabled(effective)

    def finish_onboarding(self, wizard: _OnboardingWizard, result: int) -> None:
        if wizard is not self.onboarding_wizard:
            return
        self.onboarding_cancel_event.set()
        self.signals.onboarding_test_finished.disconnect(wizard.test_page.set_result)
        self.signals.onboarding_test_progress.disconnect(wizard.test_page.set_progress)
        self.onboarding_wizard = None
        start_live = self._onboarding_start_live
        self._onboarding_start_live = False

        if result != QDialog.DialogCode.Accepted:
            self._onboarding_reading_settings = None
            self._set_reading_status(
                "Reading setup cancelled. Select Read to try again."
            )
            wizard.deleteLater()
            self.show_dashboard()
            self._restore_onboarding_runtime()
            return

        candidate = user_preferences(wizard.settings())
        try:
            dialog_region, story_region = wizard.capture_regions()
            path = (
                self._save_settings_candidate(
                    candidate, dialog_region=dialog_region, story_region=story_region
                )
                if dialog_region is not None
                else self._save_settings_candidate(candidate)
            )
        except (OSError, ValueError) as error:
            self._onboarding_reading_settings = None
            self.set_status("Setup required")
            self.show_error(f"Unable to save setup settings: {error}")
            self._set_reading_status(
                f"Unable to save Reading setup: {error}. Select Read to try again."
            )
            wizard.deleteLater()
            self.show_dashboard()
            self._restore_onboarding_runtime()
            return
        self._onboarding_preview_settings = None
        self._reading_window_retry_settings = None
        self._reading_window_hint = None
        self.settings = candidate
        reading_settings = None
        if self._onboarding_reading_settings is not None:
            reading_settings = self._onboarding_runtime_settings(candidate)
            self._retain_preparation_reading(reading_settings)
            self._onboarding_reading_settings = None
            self._bind_preparation_callbacks()
        self._update_auto_advance_action()
        # A recorded-audio test can succeed before the live reader is initialized.
        self.dashboard.set_configuration(candidate)
        self._refresh_preparation_settings()
        self.set_ready(self.controller.is_ready)
        wizard.deleteLater()
        self.show_dashboard()
        self.dashboard.show_reading()
        self.dashboard.live_button.setFocus()
        self._set_reading_status(
            "Reading setup saved. Starting narration..."
            if start_live
            else "Reading setup saved. Click Start reading when ready. "
            f"Settings saved to {path}."
        )
        self.signals.hotkeys_requested.emit()
        if start_live or (
            reading_settings is not None and wizard.start_live_after_save
        ):
            self.prepare_reading(
                reading_settings=reading_settings, start_live=start_live
            )

    def _restore_onboarding_runtime(self) -> None:
        preview_settings = self._onboarding_preview_settings
        self._onboarding_preview_settings = None
        if (
            preview_settings is not None
            and not self._shutting_down
            and self.controller.settings is preview_settings
            and preview_settings != self.settings
        ):
            self._start_configuration_apply(
                self.settings,
                progress_status="Restoring the saved Reading setup...",
                success_status="Saved Reading setup restored.",
            )

    def _save_compact_preference(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self.settings.compact_controls == enabled:
            return
        candidate = self.settings.updated(compact_controls=enabled)
        try:
            self._save_settings_candidate(candidate)
        except OSError as error:
            self.show_error(f"Unable to save compact-controls preference: {error}")
            return
        self.settings = candidate

    def _sync_active_profile(self) -> bool:
        return self._sync_profile_from_settings(
            self._last_saved_settings or self.settings
        )

    def _sync_profile_from_settings(self, settings: AppSettings) -> bool:
        profile_id = settings.active_profile_id
        if profile_id and self.profile_store.get(profile_id) is not None:
            try:
                self.profile_store.update_from_settings(profile_id, settings)
            except OSError as error:
                self.show_error(
                    "Settings were saved, but the active profile could not be "
                    f"updated: {error}"
                )
                return False
        return True

    def _recover_active_profile(self) -> bool:
        profile_id = self.settings.active_profile_id
        profile = self.profile_store.get(profile_id) if profile_id else None
        if profile is None:
            return True
        path = self._settings_path
        if not path.is_file():
            return True
        # The saved settings, not transient environment overrides, own the active snapshot.
        warnings: list[str] = []
        persisted = load_app_settings(
            path,
            environment={},
            warn=warnings.append,
            on_game_pack_error=lambda error: warnings.append(str(error)),
        )
        if warnings or persisted.active_profile_id != profile_id:
            return True
        if profile.updated_from_settings(persisted) == profile:
            return True
        return self._sync_profile_from_settings(persisted)
