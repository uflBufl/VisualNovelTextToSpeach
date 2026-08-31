"""Guided player UI for selecting content to prepare for offline speech."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from vntts.pregeneration_setup import (
    ContentDiscovery,
    PregenerationJobStore,
    PregenerationSetupError,
    discover_game_content,
    estimate_preparation,
    inspect_story_index,
)


class OfflineAudioPreparationDialog(QDialog):
    def __init__(
        self,
        settings,
        *,
        discovery=None,
        job_store=None,
        parent=None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.discovery = discovery or (lambda: discover_game_content(settings))
        self.job_store = job_store or PregenerationJobStore()
        self._content = ()
        self._job = None
        self.setWindowTitle("Prepare offline audio")
        self.setMinimumSize(700, 500)

        intro = QLabel(
            "Choose the stories you want available offline. VNTTS will reuse "
            "original game voices and ask only about ambiguous character voices."
        )
        intro.setWordWrap(True)

        self.source = QComboBox()
        self.source.setAccessibleName("Detected game content")
        self.source.setAccessibleDescription(
            "Detected local story content available for offline preparation"
        )
        self.source.currentIndexChanged.connect(self._source_changed)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.browse_button = QPushButton("Choose extracted content...")
        self.browse_button.clicked.connect(self.browse)
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Game content"))
        source_row.addWidget(self.source, 1)
        source_row.addWidget(self.refresh_button)
        source_row.addWidget(self.browse_button)

        self.source_status = QLabel()
        self.source_status.setWordWrap(True)
        self.source_status.setAccessibleName("Game content status")
        self.stories = QListWidget()
        self.stories.setAccessibleName("Stories and chapters")
        self.stories.setAccessibleDescription(
            "Check every story or chapter to prepare for offline speech"
        )
        self.stories.itemChanged.connect(self._selection_changed)

        selection_actions = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self.select_none_button = QPushButton("Select none")
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))
        selection_actions.addWidget(self.select_all_button)
        selection_actions.addWidget(self.select_none_button)
        selection_actions.addStretch()

        self.summary = QLabel("Select game content to continue.")
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Offline preparation estimate")
        self.resume_status = QLabel()
        self.resume_status.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.continue_button = self.buttons.addButton(
            "Continue",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.continue_button.setDefault(True)
        self.continue_button.setAccessibleDescription(
            "Save this selection and continue or resume offline audio preparation"
        )
        self.continue_button.clicked.connect(self._save_selection)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(source_row)
        layout.addWidget(self.source_status)
        layout.addWidget(QLabel("Stories to prepare"))
        layout.addWidget(self.stories, 1)
        layout.addLayout(selection_actions)
        layout.addWidget(self.summary)
        layout.addWidget(self.resume_status)
        layout.addWidget(self.buttons)
        self.refresh()

    def refresh(self):
        try:
            discovery = self.discovery()
        except (OSError, PregenerationSetupError) as error:
            discovery = ContentDiscovery((), (str(error),))
        if not isinstance(discovery, ContentDiscovery):
            raise TypeError("discovery must return ContentDiscovery")
        previous = self.current_content()
        previous_sha = previous.story_index_sha256 if previous else None
        self._content = discovery.content
        self.source.blockSignals(True)
        self.source.clear()
        for content in self._content:
            self.source.addItem(
                f"{content.display_name} - {content.story_index.name}",
                content.story_index_sha256,
            )
        if previous_sha:
            index = self.source.findData(previous_sha)
            if index >= 0:
                self.source.setCurrentIndex(index)
        self.source.blockSignals(False)
        if self._content:
            self.source_status.setText(
                f"Found {len(self._content)} local game source(s)."
            )
            self._source_changed(self.source.currentIndex())
        else:
            detail = f" {discovery.errors[0]}" if discovery.errors else ""
            self.source_status.setText(
                "No extracted game content was found. Run the supported game "
                "importer or choose an existing story-index file." + detail
            )
            self._populate_stories(None)

    def browse(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose extracted story content",
            "",
            "VNTTS story indexes (*.jsonl);;All files (*)",
        )
        if not path:
            return
        try:
            content = inspect_story_index(path, provider_id="selected-story-index")
        except PregenerationSetupError as error:
            self.source_status.setText(str(error))
            return
        existing = next(
            (
                index
                for index, value in enumerate(self._content)
                if value.story_index_sha256 == content.story_index_sha256
            ),
            None,
        )
        if existing is None:
            self._content = (*self._content, content)
            self.source.addItem(
                f"{content.display_name} - {content.story_index.name}",
                content.story_index_sha256,
            )
            existing = len(self._content) - 1
        self.source.setCurrentIndex(existing)
        self.source_status.setText("Selected extracted game content is ready.")

    def current_content(self):
        index = self.source.currentIndex()
        return self._content[index] if 0 <= index < len(self._content) else None

    def selected_story_ids(self):
        return tuple(
            self.stories.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.stories.count())
            if self.stories.item(row).checkState() == Qt.CheckState.Checked
        )

    def job(self):
        return self._job

    def _source_changed(self, _index):
        self._populate_stories(self.current_content())

    def _populate_stories(self, content):
        self.stories.blockSignals(True)
        self.stories.clear()
        if content is not None:
            resumed = self.job_store.latest_for_content(content)
            resumed_ids = set(resumed.selected_story_ids) if resumed else set()
            for selection in content.selections:
                item = QListWidgetItem(
                    f"{selection.title} - {selection.line_count} lines; "
                    f"{selection.generation_lines} need speech"
                )
                item.setData(Qt.ItemDataRole.UserRole, selection.selection_id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if not resumed_ids or selection.selection_id in resumed_ids
                    else Qt.CheckState.Unchecked
                )
                self.stories.addItem(item)
            self.resume_status.setText(
                "Previous selection restored. Continue resumes the same preparation."
                if resumed
                else "Your selection will be saved and can be resumed after restart."
            )
        else:
            self.resume_status.clear()
        self.stories.blockSignals(False)
        self._selection_changed()

    def _set_all_checked(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.stories.blockSignals(True)
        for row in range(self.stories.count()):
            self.stories.item(row).setCheckState(state)
        self.stories.blockSignals(False)
        self._selection_changed()

    def _selection_changed(self, _item=None):
        content = self.current_content()
        selected = self.selected_story_ids()
        if content is None:
            self.summary.setText("Choose or import game content to continue.")
            self.continue_button.setEnabled(False)
            return
        if not selected:
            self.summary.setText("Select at least one story or chapter.")
            self.continue_button.setEnabled(False)
            return
        estimate = estimate_preparation(content, selected)
        disk_megabytes = max(1, round(estimate.estimated_disk_bytes / 1_000_000))
        self.summary.setText(
            f"{estimate.selected_lines} dialogue lines selected. "
            f"{estimate.original_audio_lines} already use original game voices; "
            f"up to {estimate.generation_lines} need speech across about "
            f"{estimate.speaker_count} voices. Rough estimate: "
            f"{estimate.estimated_generation_minutes} minutes and "
            f"{disk_megabytes} MB."
        )
        self.continue_button.setEnabled(True)

    def _save_selection(self):
        content = self.current_content()
        if content is None:
            return
        try:
            self._job = self.job_store.create_or_resume(
                content,
                self.selected_story_ids(),
            )
        except (OSError, PregenerationSetupError) as error:
            self.resume_status.setText(f"Unable to save preparation: {error}")
            return
        self.accept()


__all__ = ["OfflineAudioPreparationDialog"]
