from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from vntts.ocr import get_dialog_region, get_dialog_region_file, save_dialog_region
from vntts.profiles import GameProfileStore


class GameProfilesDialog(QDialog):
    def __init__(self, settings, store=None, correction_store=None, parent=None):
        super().__init__(parent)
        self.original_settings = settings
        self.store = store or GameProfileStore.load()
        self.correction_store = correction_store
        self.selected_settings = None
        self.setWindowTitle("Game profiles")
        self.setMinimumWidth(500)

        self.profiles = QComboBox()
        self.profiles.currentIndexChanged.connect(self.update_summary)
        create_button = QPushButton("New...")
        duplicate_button = QPushButton("Duplicate...")
        rename_button = QPushButton("Rename...")
        remove_button = QPushButton("Remove")
        create_button.clicked.connect(self.create_profile)
        duplicate_button.clicked.connect(self.duplicate_profile)
        rename_button.clicked.connect(self.rename_profile)
        remove_button.clicked.connect(self.remove_profile)
        actions = QHBoxLayout()
        actions.addWidget(create_button)
        actions.addWidget(duplicate_button)
        actions.addWidget(rename_button)
        actions.addWidget(remove_button)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Profile", self.profiles)
        form.addRow("", actions)
        form.addRow("Stored settings", self.summary)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.use_button = buttons.addButton(
            "Use profile",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        buttons.accepted.connect(self.use_profile)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
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
        name = self._ask_name("New game profile", "Profile name")
        if name is None:
            return
        try:
            profile = self.store.create(
                name,
                self.original_settings,
                region=get_dialog_region(),
            )
        except ValueError as error:
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
        except ValueError as error:
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
        except ValueError as error:
            QMessageBox.warning(self, "Unable to rename profile", str(error))
            return
        self.refresh_profiles(renamed.id)

    def remove_profile(self):
        profile = self.current_profile()
        if profile is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove game profile",
            f"Remove {profile.name!r}?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.remove(profile.id)
        if self.correction_store is not None:
            self.correction_store.remove_profile(profile.id)
        self.refresh_profiles()

    def update_summary(self):
        profile = self.current_profile()
        self.use_button.setEnabled(profile is not None)
        if profile is None:
            self.summary.setText(
                "No profiles yet. Create one from the current settings."
            )
            return
        window = profile.game_window_title or "calibrated screen region"
        voice_pack = profile.voice_manifest or "default narrator"
        self.summary.setText(
            f"Window: {window}\n"
            f"OCR language: {profile.ocr_language}\n"
            f"Voice pack: {voice_pack}"
        )

    def use_profile(self):
        profile = self.current_profile()
        if profile is None:
            return
        save_dialog_region(profile.dialog_region, get_dialog_region_file())
        self.selected_settings = profile.apply(self.original_settings)
        self.accept()

    def settings(self):
        return self.selected_settings or self.original_settings

    def _ask_name(self, title, label, value=""):
        name, accepted = QInputDialog.getText(self, title, label, text=value)
        return name.strip() if accepted else None
