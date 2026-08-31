"""Background application of already-persisted desktop configuration."""

from threading import Event

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialog

from vntts.async_ui import LatestTaskRunner
from vntts.game_pack import GamePackError, apply_game_pack
from vntts.settings import (
    is_live_sequence_audio_mode,
    restart_required_setting_changes,
)


class ConfigurationApplyMixin:
    def _setup_configuration_apply(self):
        self.cancel_configuration_action = QAction("Cancel settings apply")
        self.cancel_configuration_action.setVisible(False)
        self.cancel_configuration_action.setEnabled(False)
        self.cancel_configuration_action.setStatusTip(
            "Cancel only the current runtime apply; saved settings are retained"
        )
        self.cancel_configuration_action.triggered.connect(
            self.cancel_configuration_apply
        )
        self.menu.insertAction(
            self.voice_preview_action,
            self.cancel_configuration_action,
        )
        self.configuration_runner = LatestTaskRunner(self)
        self.configuration_runner.finished.connect(self._configuration_apply_finished)
        self.configuration_runner.activeChanged.connect(
            self._configuration_runner_active_changed
        )
        self._configuration_generation = None
        self._configuration_cancellation = None
        self._configuration_success_status = None
        self._configuration_refresh_hotkeys = False

    def _configuration_runner_active_changed(self, active):
        action = getattr(self, "cancel_configuration_action", None)
        if action is None:
            return
        action.setVisible(bool(active))
        action.setEnabled(bool(active) and not self._shutting_down)

    def _start_configuration_apply(
        self,
        settings,
        *,
        progress_status,
        success_status,
        refresh_hotkeys=False,
    ):
        generation = self._begin_controller_lifecycle()
        self._configuration_generation = generation
        self._configuration_cancellation = Event()
        self._configuration_success_status = success_status
        self._configuration_refresh_hotkeys = bool(refresh_hotkeys)
        self.set_status(progress_status)
        self.configuration_runner.start(
            self._apply_configuration,
            settings,
            generation,
            self._configuration_cancellation,
        )

    def _apply_configuration(self, settings, generation, cancellation):
        applied = self.controller.apply_settings(
            settings,
            cancellation=cancellation,
        )
        return self._lifecycle_is_current(generation), applied is not False

    def cancel_configuration_apply(self):
        cancellation = self._configuration_cancellation
        if cancellation is None or not self.configuration_runner.active:
            self.set_status("No runtime configuration apply is in progress")
            return
        if self.controller.cancel_settings_apply(cancellation):
            self.cancel_configuration_action.setEnabled(False)
            self.set_status(
                "Cancelling runtime apply; saved settings remain for restart..."
            )
            return
        self.cancel_configuration_action.setEnabled(False)
        self.set_status("Runtime configuration apply is already completing")

    def _configuration_apply_finished(self, result, error):
        generation = self._configuration_generation
        self._configuration_generation = None
        self._configuration_cancellation = None
        if not self._lifecycle_is_current(generation):
            return
        self._finish_controller_lifecycle()
        if error is not None:
            self.show_error(
                "Settings were saved, but runtime reconfiguration failed: "
                f"{error}. Restart the application to apply them."
            )
            return
        current, applied = result
        if not current:
            return
        if not applied:
            self.set_status(
                "Runtime apply cancelled; saved settings will take effect after restart"
            )
            return
        if self._configuration_refresh_hotkeys:
            self.signals.hotkeys_requested.emit()
        self._configuration_refresh_hotkeys = False
        self.set_status(self._configuration_success_status)
        self._configuration_success_status = None

    def open_settings(self):
        if self._controller_busy or self._shutting_down:
            self.set_status("Controller reconfiguration is already in progress")
            return
        dialog = self._create_settings_dialog()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated_settings = dialog.settings()
        try:
            selected_new_pack = updated_settings.game_pack != self.settings.game_pack
            updated_settings = apply_game_pack(
                updated_settings,
                updated_settings.game_pack if selected_new_pack else None,
            )
        except GamePackError as error:
            self.show_error(f"Unable to import game pack: {error}")
            return
        original_settings = self.settings
        launch_changed = (
            updated_settings.launch_at_login != original_settings.launch_at_login
        )
        if launch_changed:
            try:
                self._configure_macos_launch_at_login(
                    updated_settings.launch_at_login
                )
            except OSError as error:
                self.show_error(f"Unable to configure launch at login: {error}")
                return
        try:
            path = updated_settings.save()
        except OSError as error:
            rollback_error = None
            if launch_changed:
                try:
                    self._configure_macos_launch_at_login(
                        original_settings.launch_at_login
                    )
                except OSError as caught_error:
                    rollback_error = caught_error
            message = f"Unable to save settings: {error}"
            if rollback_error is not None:
                message += f"; launch-at-login rollback also failed: {rollback_error}"
            self.show_error(message)
            return
        restart_changes = restart_required_setting_changes(
            original_settings, updated_settings
        )
        effective_backend = self.controller.settings.speech_backend
        self.settings = updated_settings
        self.dashboard.set_configuration(self.settings)
        self.sequence_resync_action.setVisible(
            is_live_sequence_audio_mode(self.settings.live_sequence_mode)
        )
        self.sequence_expected_action.setVisible(
            is_live_sequence_audio_mode(self.settings.live_sequence_mode)
        )
        self.auto_advance_action.blockSignals(True)
        self.auto_advance_action.setChecked(self.settings.auto_advance_enabled)
        self.auto_advance_action.blockSignals(False)
        profile_synced = self._sync_active_profile(updated_settings)
        profile_suffix = ""
        if not profile_synced:
            profile_suffix = "; active profile could not be updated"
        if restart_changes:
            success_status = (
                f"Settings saved to {path}; restart required to load speech "
                f"engine/model changes. This session still uses {effective_backend}"
                f"{profile_suffix}."
            )
        else:
            success_status = f"Settings saved to {path}{profile_suffix}"
        self._start_configuration_apply(
            self.settings,
            progress_status=f"Applying saved settings in background ({path})...",
            success_status=success_status,
            refresh_hotkeys=True,
        )
        if self.readiness_dialog is not None:
            self.readiness_dialog.update_settings(self.settings)

    def open_assets(self):
        if self._controller_busy or self._shutting_down:
            self.set_status("Controller reconfiguration is already in progress")
            return
        dialog = self._create_asset_manager_dialog()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        candidate = dialog.settings()
        try:
            path = candidate.save()
        except OSError as error:
            self.show_error(f"Unable to save model and voice settings: {error}")
            return
        self.settings = candidate
        profile_synced = self._sync_active_profile(candidate)
        profile_suffix = ""
        if not profile_synced:
            profile_suffix = "; active profile could not be updated"
        self._start_configuration_apply(
            self.settings,
            progress_status="Applying model and voice settings in background...",
            success_status=(
                "Assets updated; restart to load voice or model changes. "
                f"Saved to {path}{profile_suffix}"
            ),
        )
