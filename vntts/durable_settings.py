"""Failure-atomic settings mutations shared by the desktop application shell."""

from PySide6.QtWidgets import QDialog


class DurableSettingsMixin:
    """Persist settings candidates before publishing them to runtime state."""

    def toggle_auto_advance(self, enabled):
        candidate = self.settings.updated(auto_advance_enabled=bool(enabled))
        try:
            candidate.save()
        except OSError as error:
            self.auto_advance_action.blockSignals(True)
            self.auto_advance_action.setChecked(self.settings.auto_advance_enabled)
            self.auto_advance_action.blockSignals(False)
            self.show_error(f"Unable to save auto-advance setting: {error}")
            return
        self.settings = candidate
        self.controller.set_auto_advance_enabled(enabled)

    def update_profile_region(self, region):
        profile_id = self.settings.active_profile_id
        if profile_id and self.profile_store.get(profile_id) is not None:
            try:
                self.profile_store.update_region(profile_id, region)
            except OSError as error:
                self.show_error(
                    f"Unable to save the calibrated profile region: {error}"
                )

    def finish_onboarding(self, wizard, result):
        if wizard is not self.onboarding_wizard:
            return
        self.onboarding_cancel_event.set()
        self.signals.onboarding_test_finished.disconnect(wizard.test_page.set_result)
        self.signals.onboarding_test_progress.disconnect(wizard.test_page.set_progress)
        self.onboarding_wizard = None

        if result != QDialog.DialogCode.Accepted:
            self.set_status("Setup required")
            wizard.deleteLater()
            return

        candidate = wizard.settings()
        try:
            path = candidate.save()
        except OSError as error:
            self.set_status("Setup required")
            self.show_error(f"Unable to save setup settings: {error}")
            wizard.deleteLater()
            return
        self.settings = candidate
        self.controller.apply_settings(candidate)
        self.set_ready(self.controller.is_ready)
        self.set_status(f"Setup completed; settings saved to {path}")
        wizard.deleteLater()
        self.signals.hotkeys_requested.emit()

    def _save_compact_preference(self, enabled):
        enabled = bool(enabled)
        if self.settings.compact_controls == enabled:
            return
        candidate = self.settings.updated(compact_controls=enabled)
        try:
            candidate.save()
        except OSError as error:
            self.show_error(f"Unable to save compact-controls preference: {error}")
            return
        self.settings = candidate

    def assign_voice(self, character, source_id):
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

    def clear_voice_assignment(self, character):
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

    def set_force_live_narrator(self, enabled):
        path, suffix = self._persist_voice_change(
            lambda commit: self.controller.set_force_live_narrator(
                enabled,
                commit_settings=commit,
            ),
            "Unable to save Narrator routing",
        )
        self.set_status(f"Narrator routing saved to {path}{suffix}")
        return self.settings

    def _persist_voice_change(self, operation, failure_message):
        saved_path = []
        try:
            settings = operation(lambda candidate: saved_path.append(candidate.save()))
        except OSError as error:
            self.show_error(f"{failure_message}: {error}")
            raise
        self.settings = settings
        profile_synced = self._sync_active_profile(self.settings)
        suffix = "" if profile_synced else "; active profile could not be updated"
        return saved_path[0], suffix

    def _sync_active_profile(self, settings=None):
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
