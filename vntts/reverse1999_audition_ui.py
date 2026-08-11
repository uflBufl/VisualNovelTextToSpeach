import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vntts.reverse1999_audition import (
    candidate_banks,
    default_bank_index,
    default_dialogue_index,
    filter_dialogue,
    load_audition_data,
    prepare_audition_clip,
    save_speaker_mapping,
)


class Reverse1999AuditionDialog(QDialog):
    def __init__(
        self,
        dialogue_index,
        bank_index,
        *,
        clip_preparer=prepare_audition_clip,
        mapping_saver=save_speaker_mapping,
        parent=None,
    ):
        super().__init__(parent)
        self.dialogue_index = dialogue_index
        self.bank_index = bank_index
        self.clip_preparer = clip_preparer
        self.mapping_saver = mapping_saver
        self.candidates = []
        self.setWindowTitle("Reverse: 1999 speaker audition")
        self.setMinimumSize(1000, 650)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Speaker name, NPC ID, or dialogue text")
        self.chapter = QComboBox()
        self.chapter.addItem("All chapters", None)
        chapters = sorted(
            {str(row.get("chapter")) for row in dialogue_index.get("dialogue", [])}
        )
        for chapter in chapters:
            self.chapter.addItem(chapter, chapter)
        self.search.textChanged.connect(self.refresh_dialogue)
        self.chapter.currentIndexChanged.connect(self.refresh_dialogue)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Find"))
        filters.addWidget(self.search, 2)
        filters.addWidget(QLabel("Chapter"))
        filters.addWidget(self.chapter, 1)

        self.dialogue = QTableWidget(0, 4)
        self.dialogue.setHorizontalHeaderLabels(
            ["Chapter", "Sequence", "Speaker", "Dialogue evidence"]
        )
        self.dialogue.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.dialogue.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.dialogue.horizontalHeader().setStretchLastSection(True)
        self.dialogue.itemSelectionChanged.connect(self.dialogue_selected)

        self.banks = QListWidget()
        self.banks.currentRowChanged.connect(self.bank_selected)
        self.media = QComboBox()
        self.play_button = QPushButton("Play selected clip")
        self.stop_button = QPushButton("Stop")
        self.play_button.clicked.connect(self.play_clip)
        self.stop_button.clicked.connect(self.stop_clip)
        player_actions = QHBoxLayout()
        player_actions.addWidget(self.media, 2)
        player_actions.addWidget(self.play_button)
        player_actions.addWidget(self.stop_button)

        bank_panel = QWidget()
        bank_layout = QVBoxLayout(bank_panel)
        bank_layout.addWidget(QLabel("Chapter-aware voice-bank candidates"))
        bank_layout.addWidget(self.banks, 1)
        bank_layout.addLayout(player_actions)

        evidence_panel = QWidget()
        evidence_layout = QVBoxLayout(evidence_panel)
        evidence_layout.addWidget(QLabel("Dialogue evidence"))
        evidence_layout.addWidget(self.dialogue)

        splitter = QSplitter()
        splitter.addWidget(evidence_panel)
        splitter.addWidget(bank_panel)
        splitter.setSizes([600, 400])

        self.speaker_name = QLineEdit()
        self.npc_id = QLineEdit()
        self.save_button = QPushButton("Save local speaker mapping")
        self.save_button.clicked.connect(self.save_mapping)
        mapping = QFormLayout()
        mapping.addRow("Speaker name", self.speaker_name)
        mapping.addRow("NPC ID", self.npc_id)
        mapping.addRow("", self.save_button)

        self.status = QLabel(
            "Select dialogue evidence, audition matching chapter banks, then save "
            "the confirmed speaker mapping."
        )
        self.status.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addLayout(filters)
        layout.addWidget(splitter, 1)
        layout.addLayout(mapping)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.refresh_dialogue()

    def refresh_dialogue(self):
        rows = filter_dialogue(
            self.dialogue_index.get("dialogue", []),
            query=self.search.text(),
            chapter=self.chapter.currentData(),
        )
        self.dialogue.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            speaker = row.get("speaker_name") or f"Unknown ({row.get('speaker_id')})"
            values = (
                row.get("chapter", ""),
                row.get("sequence", ""),
                speaker,
                row.get("text", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(256, row)
                self.dialogue.setItem(row_index, column, item)
        self.status.setText(f"Showing {len(rows)} dialogue evidence row(s).")
        self.banks.clear()
        self.media.clear()

    def dialogue_selected(self):
        selected = self.dialogue.selectedItems()
        if not selected:
            return
        row = selected[0].data(256)
        chapter = row.get("chapter")
        speaker_id = row.get("speaker_id")
        if row.get("speaker_name"):
            self.speaker_name.setText(row["speaker_name"])
        self.npc_id.setText(str(speaker_id or ""))
        self.candidates = candidate_banks(
            self.bank_index, chapter=chapter, speaker_id=speaker_id
        )
        self.banks.clear()
        for candidate in self.candidates:
            npc = ", ".join(candidate.npc_ids) or "unknown NPC"
            self.banks.addItem(f"{candidate.filename}  [{npc}]")
        self.status.setText(
            f"Found {len(self.candidates)} candidate bank(s) for chapter {chapter}."
        )
        if self.candidates:
            self.banks.setCurrentRow(0)

    def bank_selected(self, index):
        self.media.clear()
        if index < 0 or index >= len(self.candidates):
            return
        candidate = self.candidates[index]
        for media_id in candidate.media_ids:
            self.media.addItem(str(media_id), media_id)
        if len(candidate.npc_ids) == 1:
            self.npc_id.setText(candidate.npc_ids[0])

    def selected_bank(self):
        index = self.banks.currentRow()
        if index < 0 or index >= len(self.candidates):
            return None
        return self.candidates[index]

    def play_clip(self):
        candidate = self.selected_bank()
        media_id = self.media.currentData()
        if candidate is None or media_id is None:
            self.status.setText("Choose a bank and media clip first.")
            return
        root = Path(self.bank_index["game_audio_directory"])
        try:
            output = self.clip_preparer(root / candidate.path, media_id)
        except Exception as error:
            self.status.setText(f"Unable to prepare clip: {error}")
            return
        self.player.setSource(QUrl.fromLocalFile(str(output)))
        self.player.play()
        self.status.setText(f"Playing {candidate.filename} / {media_id}")

    def stop_clip(self):
        self.player.stop()
        self.status.setText("Playback stopped.")

    def save_mapping(self):
        candidate = self.selected_bank()
        if candidate is None:
            self.status.setText("Choose the confirmed voice bank first.")
            return
        selected = self.dialogue.selectedItems()
        chapter = selected[0].data(256).get("chapter") if selected else ""
        try:
            path = self.mapping_saver(
                self.speaker_name.text(),
                self.npc_id.text(),
                candidate.filename,
                chapter,
            )
        except Exception as error:
            self.status.setText(f"Unable to save mapping: {error}")
            return
        self.status.setText(f"Saved local speaker mapping to {path}")


def create_parser():
    parser = argparse.ArgumentParser(
        description="Review and audition unresolved Reverse: 1999 NPC voices."
    )
    parser.add_argument("--dialogue-index", type=Path, default=default_dialogue_index)
    parser.add_argument("--bank-index", type=Path, default=default_bank_index)
    parser.add_argument("--search", default="")
    return parser


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    _application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialogue_index, bank_index = load_audition_data(
            arguments.dialogue_index, arguments.bank_index
        )
    except Exception as error:
        QMessageBox.critical(None, "Unable to open speaker audition", str(error))
        return 1
    dialog = Reverse1999AuditionDialog(dialogue_index, bank_index)
    dialog.search.setText(arguments.search)
    dialog.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
