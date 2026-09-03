from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)
from vntts_artifacts.game_pack import GamePackError

from vntts.ocr import get_dialog_region, get_dialog_region_file, save_dialog_region
from vntts.profiles import GameProfileStore


class GameProfilesDialog(QDialog):
    def __init__(self, settings, store=None, correction_store=None, parent=None):
        super().__init__(parent)
        self.original_settings = settings
        self.active_profile_id = settings.active_profile_id
        self.store = store or GameProfileStore.load()
        self.correction_store = correction_store
        self.selected_settings = None
        self.setWindowTitle("Game profiles")
        self.setMinimumWidth(500)

        self.profiles = QComboBox()
        self.profiles.currentIndexChanged.connect(self.update_summary)
        self.create_button = QPushButton("Save current setup as profile...")
        self.duplicate_button = QPushButton("Duplicate...")
        self.rename_button = QPushButton("Rename...")
        self.remove_button = QPushButton("Remove selected profile")
        self.create_button.clicked.connect(self.create_profile)
        self.duplicate_button.clicked.connect(self.duplicate_profile)
        self.rename_button.clicked.connect(self.rename_profile)
        self.remove_button.clicked.connect(self.remove_profile)
        self.create_button.setAccessibleName("Save current setup as a game profile")
        self.duplicate_button.setAccessibleName("Duplicate selected game profile")
        self.rename_button.setAccessibleName("Rename selected game profile")
        self.remove_button.setAccessibleName("Remove selected game profile")
        actions = QHBoxLayout()
        actions.addWidget(self.create_button)
        actions.addWidget(self.duplicate_button)
        actions.addWidget(self.rename_button)
        actions.addStretch()
        actions.addWidget(self.remove_button)
        management = QGroupBox("Manage stored profiles")
        management.setLayout(actions)

        self.active_status = QLabel()
        self.active_status.setAccessibleName("Active game profile")
        self.active_status.setWordWrap(True)
        self.summary = QLabel()
        self.summary.setAccessibleName("Selected game profile settings")
        self.summary.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Active", self.active_status)
        form.addRow("Profile", self.profiles)
        form.addRow("Stored settings", self.summary)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.use_button = buttons.addButton(
            "Use selected profile",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.use_button.setAccessibleName("Activate selected game profile")
        buttons.accepted.connect(self.use_profile)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(management)
        layout.addWidget(buttons)
        self.refresh_profiles(settings.active_profile_id)

    def refresh_profiles(self, selected_id=None):
        self.profiles.clear()
        for profile in sorted(
            self.store.profiles, key=lambda item: item.name.casefold()
        ):
            self.profiles.addItem(profile.name, profile.id)
        if selected_id:
            index = self.profiles.findData(selected_id)
            if index >= 0:
                self.profiles.setCurrentIndex(index)
        self.update_summary()

    def current_profile(self):
        return self.store.get(self.profiles.currentData())

    def create_profile(self):
        name = self._ask_name("Save current setup as profile", "Profile name")
        if name is None:
            return
        try:
            profile = self.store.create(
                name,
                self.original_settings,
                region=get_dialog_region(),
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Unable to create profile", str(error))
            return
        self.refresh_profiles(profile.id)

    def duplicate_profile(self):
        profile = self.current_profile()
        if profile is None:
            return
        name = self._ask_name(
            "Duplicate game profile",
            "New profile name",
            f"{profile.name} copy",
        )
        if name is None:
            return
        try:
            duplicate = self.store.duplicate(profile.id, name)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Unable to duplicate profile", str(error))
            return
        if self.correction_store is not None:
            self.correction_store.copy_profile(profile.id, duplicate.id)
        self.refresh_profiles(duplicate.id)

    def rename_profile(self):
        profile = self.current_profile()
        if profile is None:
            return
        name = self._ask_name(
            "Rename game profile",
            "Profile name",
            profile.name,
        )
        if name is None:
            return
        try:
            renamed = self.store.rename(profile.id, name)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Unable to rename profile", str(error))
            return
        self.refresh_profiles(renamed.id)

    def remove_profile(self):
        profile = self.current_profile()
        if profile is None:
            return
        correction_count = (
            len(self.correction_store.profile_entries.get(str(profile.id), {}))
            if self.correction_store is not None
            else 0
        )
        prompt = QMessageBox(
            QMessageBox.Icon.Warning,
            "Remove game profile",
            "",
            parent=self,
        )
        prompt.setText(f"Remove {profile.name!r}?")
        prompt.setInformativeText(
            "This permanently deletes the profile and "
            f"{correction_count} profile-scoped OCR "
            f"correction{'s' if correction_count != 1 else ''}."
        )
        remove_button = prompt.addButton(
            "Remove profile",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel_button = prompt.addButton(QMessageBox.StandardButton.Cancel)
        prompt.setDefaultButton(cancel_button)
        prompt.setEscapeButton(cancel_button)
        prompt.exec()
        if prompt.clickedButton() is not remove_button:
            return
        try:
            self.store.remove(profile.id)
        except OSError as error:
            QMessageBox.warning(self, "Unable to remove profile", str(error))
            return
        if self.correction_store is not None:
            self.correction_store.remove_profile(profile.id)
        self.refresh_profiles()

    def update_summary(self):
        profile = self.current_profile()
        active = self.store.get(self.active_profile_id)
        self.active_status.setText(
            active.name if active is not None else "No stored profile is active"
        )
        selected_is_active = (
            profile is not None and profile.id == self.active_profile_id
        )
        self.use_button.setEnabled(profile is not None and not selected_is_active)
        self.use_button.setText(
            "Already active" if selected_is_active else "Use selected profile"
        )
        self.use_button.setToolTip(
            "This profile is already active"
            if selected_is_active
            else "Apply the selected stored settings and make this profile active"
        )
        has_profile = profile is not None
        self.duplicate_button.setEnabled(has_profile)
        self.rename_button.setEnabled(has_profile)
        self.remove_button.setEnabled(has_profile and not selected_is_active)
        self.remove_button.setToolTip(
            "Activate another profile before removing the active profile"
            if selected_is_active
            else "Permanently remove the selected stored profile"
        )
        if profile is None:
            self.summary.setText(
                "No profiles yet. Save the current setup as a profile."
            )
            return
        capture = (
            f"window '{profile.game_window_title}'"
            if profile.capture_mode == "window" and profile.game_window_title
            else "full screen"
        )
        content = Path(profile.game_pack).name if profile.game_pack else "no game pack"
        prepared = (
            Path(profile.generated_audio_manifest).name
            if profile.generated_audio_manifest
            else "none"
        )
        voice_pack = (
            Path(profile.voice_manifest).name
            if profile.voice_manifest
            else "default narrator"
        )
        narrator_routing = (
            "always live" if profile.force_live_narrator else "prepared audio first"
        )
        self.summary.setText(
            f"Selected: {profile.name}"
            f"{' (active)' if selected_is_active else ' (not active)'}\n"
            "Activation applies:\n"
            f"- Capture: {capture} with its calibrated dialogue area\n"
            f"- Content: {content}; sequence mode {profile.live_sequence_mode}\n"
            f"- Audio: {profile.audio_source_policy}; prepared tracks {prepared}\n"
            f"- Voices: {voice_pack}; {len(profile.voice_assignments)} manual "
            f"assignment(s); Narrator {narrator_routing}\n"
            f"OCR language: {profile.ocr_language}\n"
        )

    def use_profile(self):
        profile = self.current_profile()
        if profile is None:
            return
        try:
            selected_settings = profile.apply(self.original_settings)
        except GamePackError as error:
            QMessageBox.warning(self, "Unable to use profile", str(error))
            return
        save_dialog_region(profile.dialog_region, get_dialog_region_file())
        self.selected_settings = selected_settings
        self.active_profile_id = profile.id
        self.accept()

    def settings(self):
        return self.selected_settings or self.original_settings

    def _ask_name(self, title, label, value=""):
        name, accepted = QInputDialog.getText(self, title, label, text=value)
        return name.strip() if accepted else None
