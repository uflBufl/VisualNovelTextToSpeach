"""Failure-atomic settings mutations shared by the desktop application shell."""

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Protocol, TypeAlias

from PySide6.QtWidgets import QDialog

from vntts.auto_advance_policy import (
    auto_advance_control_state,
    guard_auto_advance_settings,
)
from vntts.configuration_apply import (
    SettingsCommit,
    _Controller,
    _Dashboard,
    _Signals,
)
from vntts.ocr import DialogRegion
from vntts.profiles import GameProfile
from vntts.settings import (
    AppSettings,
    load_app_settings,
    settings_schema_version,
)
from vntts.versioned_json import write_versioned_json_if_unchanged

VoiceChange: TypeAlias = Callable[[SettingsCommit], AppSettings]


class _OnboardingTestPage(Protocol):
    def set_result(self, succeeded: bool, message: str) -> None: ...

    def set_progress(self, percent: int | None, message: str) -> None: ...


class _OnboardingWizard(Protocol):
    test_page: _OnboardingTestPage

    def settings(self) -> AppSettings: ...

    def deleteLater(self) -> None: ...


class _ProfileStore(Protocol):
    def get(self, profile_id: str) -> GameProfile | None: ...

    def update_region(self, profile_id: str, region: DialogRegion) -> object: ...

    def update_from_settings(
        self, profile_id: str, settings: AppSettings
    ) -> object: ...


class DurableSettingsMixin:
    """Persist settings candidates before publishing them to runtime state."""

    if TYPE_CHECKING:
        settings: AppSettings
        controller: _Controller
        dashboard: _Dashboard
        profile_store: _ProfileStore
        signals: _Signals
        onboarding_wizard: _OnboardingWizard | None
        onboarding_cancel_event: Event
        _settings_path: Path
        _settings_revision: bytes | None
        _settings_commit_lock: Lock

        def _update_auto_advance_action(self) -> None: ...

        def _refresh_preparation_settings(self) -> None: ...

        def _apply_controller_action_state(self) -> None: ...

        def set_status(self, message: str | None) -> None: ...

        def set_ready(self, ready: bool) -> None: ...

        def show_error(self, message: str) -> None: ...

        def show_dashboard(self) -> None: ...

    def _save_settings_candidate(self, candidate: AppSettings) -> Path:
        with self._settings_commit_lock:
            self._settings_revision = write_versioned_json_if_unchanged(
                self._settings_path,
                settings_schema_version,
                asdict(candidate),
                revision=self._settings_revision,
                document_name="Settings",
            )
            return self._settings_path

    def toggle_auto_advance(self, enabled: bool) -> None:
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

    def update_profile_region(self, region: DialogRegion) -> None:
        profile_id = self.settings.active_profile_id
        if profile_id and self.profile_store.get(profile_id) is not None:
            try:
                self.profile_store.update_region(profile_id, region)
            except OSError as error:
                self.show_error(
                    f"Unable to save the calibrated profile region: {error}"
                )

    def finish_onboarding(self, wizard: _OnboardingWizard, result: int) -> None:
        if wizard is not self.onboarding_wizard:
            return
        self.onboarding_cancel_event.set()
        self.signals.onboarding_test_finished.disconnect(wizard.test_page.set_result)
        self.signals.onboarding_test_progress.disconnect(wizard.test_page.set_progress)
        self.onboarding_wizard = None

        if result != QDialog.DialogCode.Accepted:
            self.set_status("Setup required")
            wizard.deleteLater()
            self.show_dashboard()
            return

        candidate = wizard.settings().updated(
            last_main_section=self.settings.last_main_section
        )
        try:
            path = self._save_settings_candidate(candidate)
        except OSError as error:
            self.set_status("Setup required")
            self.show_error(f"Unable to save setup settings: {error}")
            wizard.deleteLater()
            self.show_dashboard()
            return
        self.settings = candidate
        self._update_auto_advance_action()
        self.controller.apply_settings(candidate)
        self.dashboard.set_configuration(candidate)
        self._refresh_preparation_settings()
        self.set_ready(self.controller.is_ready)
        wizard.deleteLater()
        self.show_dashboard()
        self.dashboard.show_reading()
        self.dashboard.live_button.setFocus()
        self.set_status(
            "Reading setup saved. Click Start reading when ready. "
            f"Settings saved to {path}."
        )
        self.signals.hotkeys_requested.emit()

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

    def _save_main_section(self, section: str) -> None:
        if self.settings.last_main_section == section:
            return
        candidate = self.settings.updated(last_main_section=section)
        try:
            self._save_settings_candidate(candidate)
        except OSError as error:
            self.show_error(f"Unable to save the selected main section: {error}")
            return
        self.settings = candidate

    def assign_voice(self, character: str, source_id: str) -> AppSettings:
        path, suffix = self._persist_voice_change(
            lambda commit: self.controller.assign_voice(
                character,
                source_id,
                commit_settings=commit,
            ),
            f"Unable to save the voice for {character}",
        )
        self.set_status(f"Voice for {character} saved to {path}{suffix}")
        return self.settings

    def clear_voice_assignment(self, character: str) -> AppSettings:
        path, suffix = self._persist_voice_change(
            lambda commit: self.controller.clear_voice_assignment(
                character,
                commit_settings=commit,
            ),
            f"Unable to save automatic voice routing for {character}",
        )
        self.set_status(
            f"Automatic voice routing for {character} saved to {path}{suffix}"
        )
        return self.settings

    def set_force_live_narrator(self, enabled: bool) -> AppSettings:
        path, suffix = self._persist_voice_change(
            lambda commit: self.controller.set_force_live_narrator(
                enabled,
                commit_settings=commit,
            ),
            "Unable to save Narrator routing",
        )
        self.set_status(f"Narrator routing saved to {path}{suffix}")
        return self.settings

    def _persist_voice_change(
        self, operation: VoiceChange, failure_message: str
    ) -> tuple[Path, str]:
        saved_path: list[Path] = []
        section = self.settings.last_main_section

        def commit(candidate: AppSettings) -> Path:
            path = self._save_settings_candidate(
                candidate.updated(last_main_section=section)
            )
            saved_path.append(path)
            return path

        try:
            settings = operation(commit)
        except OSError as error:
            self.show_error(f"{failure_message}: {error}")
            raise
        self.settings = settings.updated(last_main_section=section)
        self.dashboard.set_configuration(self.settings)
        self._refresh_preparation_settings()
        self._apply_controller_action_state()
        profile_synced = self._sync_active_profile(self.settings)
        suffix = "" if profile_synced else "; active profile could not be updated"
        return saved_path[0], suffix

    def _sync_active_profile(self, settings: AppSettings | None = None) -> bool:
        settings = self.settings if settings is None else settings
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
        return self._sync_active_profile(persisted)
