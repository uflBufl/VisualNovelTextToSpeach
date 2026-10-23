from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.game_pack import GamePackError

from vntts.ocr import get_dialog_region, get_ocr_regions
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.profiles import GameProfile, GameProfileStore
from vntts.settings import AppSettings
from vntts.ui_text import copy_text_button, make_text_copyable, set_labeled_text


class GameProfilesDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        store: GameProfileStore | None = None,
        correction_store: OCRCorrectionStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.original_settings = settings
        self.active_profile_id = settings.active_profile_id
        self.store = store or GameProfileStore.load()
        self.correction_store = correction_store
        self.selected_settings: AppSettings | None = None
        self.setWindowTitle("Game profiles")
        self.setMinimumWidth(500)
        if self.store.profiles:
            self.resize(600, 440)
        else:
            self.resize(500, 200)

        management = self._create_profile_controls()
        self._create_summary()
        buttons = self._create_footer()
        self._create_layout(management, buttons)
        make_text_copyable(self)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.refresh_profiles(settings.active_profile_id)

    def _create_profile_controls(self) -> QWidget:
        self.profiles = QComboBox()
        self.profiles.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.profiles.setMinimumContentsLength(20)
        self.profiles.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.profiles.setAccessibleName("Selected game profile")
        self.profiles.currentIndexChanged.connect(self.update_summary)
        self.create_button = QPushButton("Save current setup...")
        self.duplicate_button = QPushButton("Duplicate...")
        self.rename_button = QPushButton("Rename...")
        self.remove_button = QPushButton("Remove")
        self.create_button.clicked.connect(self.create_profile)
        self.duplicate_button.clicked.connect(self.duplicate_profile)
        self.rename_button.clicked.connect(self.rename_profile)
        self.remove_button.clicked.connect(self.remove_profile)
        self.create_button.setAccessibleName("Save current setup as a game profile")
        self.duplicate_button.setAccessibleName("Duplicate selected game profile")
        self.rename_button.setAccessibleName("Rename selected game profile")
        self.remove_button.setAccessibleName("Remove selected game profile")
        actions = QHBoxLayout()
        actions.addWidget(self.duplicate_button)
        actions.addWidget(self.rename_button)
        actions.addWidget(self.remove_button)
        actions.addStretch()
        self.profile_actions = QWidget()
        self.profile_actions.setLayout(actions)
        actions.setContentsMargins(0, 0, 0, 0)
        management_layout = QVBoxLayout()
        management_layout.addWidget(self.profile_actions)
        management_layout.addWidget(
            self.create_button, alignment=Qt.AlignmentFlag.AlignLeft
        )
        management = QWidget()
        management.setLayout(management_layout)
        management_layout.setContentsMargins(0, 0, 0, 0)

        return management

    def _create_summary(self) -> None:
        self.active_status = QLabel()
        self.active_status.setAccessibleName("Active game profile")
        self.active_status.setWordWrap(True)
        self.active_status.setTextFormat(Qt.TextFormat.PlainText)
        self.summary = QLabel()
        self.summary.setAccessibleName("Selected game profile settings")
        self.summary.setWordWrap(True)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Active", self.active_status)
        form.addRow("Selected profile", self.profiles)
        self.profile_selection = QWidget()
        self.profile_selection.setLayout(form)
        form.setContentsMargins(0, 0, 0, 0)
        self.summary_scroll = QScrollArea()
        self.summary_scroll.setWidgetResizable(True)
        self.summary_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.summary_scroll.setWidget(self.summary)
        self.copy_settings = copy_text_button("Copy settings", self.summary.toolTip)
        self.summary_heading = QWidget()
        heading = QHBoxLayout(self.summary_heading)
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addWidget(QLabel("Selected profile settings"))
        heading.addStretch()
        heading.addWidget(self.copy_settings)

    def _create_footer(self) -> QDialogButtonBox:
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.use_button = buttons.addButton(
            "Use selected profile",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.use_button.setAccessibleName("Activate selected game profile")
        buttons.accepted.connect(self.use_profile)
        buttons.rejected.connect(self.reject)
        self.persistence_note = QLabel("Profile changes are saved immediately.")
        self.persistence_note.setWordWrap(True)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        for button in (
            self.create_button,
            self.duplicate_button,
            self.rename_button,
            self.remove_button,
            self.copy_settings,
            self.use_button,
            self.close_button,
        ):
            button.setAutoDefault(False)

        return buttons

    def _create_layout(self, management: QWidget, buttons: QDialogButtonBox) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self.profile_selection)
        layout.addWidget(self.summary_heading)
        layout.addWidget(self.summary_scroll, 1)
        layout.addWidget(management)
        layout.addWidget(self.persistence_note)
        layout.addWidget(buttons)

    def refresh_profiles(self, selected_id: str | None = None) -> None:
        was_empty = self.profiles.count() == 0
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
        if was_empty != (self.profiles.count() == 0):
            if self.store.profiles:
                self.resize(600, 440)
            else:
                self.resize(500, 200)

    def current_profile(self) -> GameProfile | None:
        profile_id = self.profiles.currentData()
        return self.store.get(profile_id) if isinstance(profile_id, str) else None

    def create_profile(self) -> None:
        name = self._ask_name("Save current setup as profile", "Profile name")
        if name is None:
            return
        active = self.store.get(self.original_settings.active_profile_id)
        if active is not None:
            region = active.dialog_region
            ocr_regions = active.ocr_regions
        else:
            region = get_dialog_region()
            ocr_regions = get_ocr_regions(region)
        try:
            profile = self.store.create(
                name,
                self.original_settings,
                region=region,
                ocr_regions=ocr_regions,
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Unable to create profile", str(error))
            return
        self.refresh_profiles(profile.id)

    def duplicate_profile(self) -> None:
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
        duplicate: GameProfile | None = None
        try:
            duplicate = self.store.duplicate(profile.id, name)
            if self.correction_store is not None:
                self.correction_store.copy_profile(profile.id, duplicate.id)
        except (OSError, ValueError) as error:
            if duplicate is not None:
                try:
                    self.store.remove(duplicate.id)
                except OSError as rollback_error:
                    error = OSError(
                        f"{error}; duplicate cleanup failed: {rollback_error}"
                    )
            QMessageBox.warning(self, "Unable to duplicate profile", str(error))
            self.refresh_profiles(profile.id)
            return
        self.refresh_profiles(duplicate.id)

    def rename_profile(self) -> None:
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

    def remove_profile(self) -> None:
        profile = self.current_profile()
        if profile is None or profile.id == self.active_profile_id:
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
            try:
                self.correction_store.remove_profile(profile.id)
            except OSError as error:
                try:
                    self.store.restore(profile)
                except OSError as rollback_error:
                    QMessageBox.warning(
                        self,
                        "Profile removal incomplete",
                        f"OCR corrections remain after profile removal: {error}. "
                        f"Restoring the profile also failed: {rollback_error}",
                    )
                else:
                    QMessageBox.warning(
                        self,
                        "Profile removal cancelled",
                        f"OCR corrections could not be removed: {error}. "
                        "The profile was restored; retry removal later.",
                    )
                self.refresh_profiles(profile.id)
                return
        self.refresh_profiles()

    def update_summary(self) -> None:
        profile = self.current_profile()
        active = (
            self.store.get(self.active_profile_id) if self.active_profile_id else None
        )
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
        self.profile_selection.setVisible(has_profile)
        self.summary_heading.setVisible(has_profile)
        self.profile_actions.setVisible(has_profile)
        self.use_button.setVisible(has_profile)
        self.persistence_note.setVisible(has_profile)
        self.profiles.setToolTip(profile.name if profile else "")
        self.create_button.setDefault(not has_profile)
        self.close_button.setDefault(selected_is_active)
        self.use_button.setDefault(has_profile and not selected_is_active)
        self.duplicate_button.setEnabled(has_profile)
        self.rename_button.setEnabled(has_profile)
        self.remove_button.setEnabled(has_profile and not selected_is_active)
        self.remove_button.setToolTip(
            "Activate another profile before removing the active profile"
            if selected_is_active
            else "Permanently remove the selected stored profile"
        )
        if profile is None:
            self.summary.setTextFormat(Qt.TextFormat.PlainText)
            self.summary.setText(
                "No profiles yet. Save your current game setup to reuse its "
                "capture, audio and reading settings."
            )
            self.summary.setToolTip("")
            return
        capture = (
            (
                f"Window: {profile.game_window_title}"
                if profile.game_window_title
                else "Window not selected - choose one in Settings"
            )
            if profile.capture_mode == "window"
            else "Full screen"
        )
        capture += "; saved dialogue area"
        narrator_routing = (
            "Always live TTS" if profile.force_live_narrator else "Follows audio source"
        )
        audio_source = {
            "live-tts-only": "Live TTS only",
            "prefer-generated": "Generated audio, then live TTS",
            "prefer-game-audio": "Original game audio, then generated/live TTS",
        }.get(profile.audio_source_policy, profile.audio_source_policy)
        sequence = {
            "off": "Disabled",
            "shadow": "Diagnostics only",
            "audio-auto": "Automatic prepared-audio advance",
            "audio-manual": "Manual prepared-audio recovery",
        }.get(profile.live_sequence_mode, profile.live_sequence_mode)
        if profile.live_sequence_mode != "off" and not profile.live_sequence_plan:
            sequence += (
                "; from game pack if available"
                if profile.game_pack
                else "; no timing plan saved"
            )
        language = (
            "English (eng)" if profile.ocr_language == "eng" else profile.ocr_language
        )
        file_rows = (
            (
                "Game content",
                profile.game_pack or profile.story_index or "Not configured",
            ),
            (
                "Prepared audio",
                "From game pack"
                if profile.game_pack
                else profile.generated_audio_manifest or "None",
            ),
            (
                "Character voices",
                "From game pack"
                if profile.game_pack
                else profile.voice_manifest or "Default narrator",
            ),
        )
        rows = (
            ("Capture", capture),
            ("OCR language", language),
            ("Audio source", audio_source),
            ("Narrator", narrator_routing),
            ("Story timing", sequence),
        )
        set_labeled_text(
            self.summary,
            (*rows, *((name, Path(value).name) for name, value in file_rows)),
        )
        self.summary.setToolTip(
            f"Profile: {profile.name}\n"
            + "\n".join(f"{name}: {value}" for name, value in (*rows, *file_rows))
        )
        self.summary_scroll.verticalScrollBar().setValue(0)

    def use_profile(self) -> None:
        profile = self.current_profile()
        if profile is None:
            return
        try:
            selected_settings = profile.apply(self.original_settings)
        except GamePackError as error:
            QMessageBox.warning(self, "Unable to use profile", str(error))
            return
        self.selected_settings = selected_settings
        self.active_profile_id = profile.id
        self.accept()

    def settings(self) -> AppSettings:
        return self.selected_settings or self.original_settings

    def _ask_name(self, title: str, label: str, value: str = "") -> str | None:
        name, accepted = QInputDialog.getText(self, title, label, text=value)
        return name.strip() if accepted else None
