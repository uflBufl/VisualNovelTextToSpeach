#!/usr/bin/env python3
"""Render a small Storybook-like catalog from real VNTTS Qt widgets."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "ui-catalog.json"
DEFAULT_OUTPUT = PROJECT_ROOT / ".codex" / "ui-catalog"


def load_catalog(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("ui-catalog.json must use schema_version 1")
    contracts = document.get("contracts")
    surfaces = document.get("surfaces")
    if not isinstance(contracts, dict) or not isinstance(surfaces, list):
        raise ValueError("catalog requires contracts and surfaces")

    surface_ids: set[str] = set()
    story_ids: set[str] = set()
    for surface in surfaces:
        surface_id = _required_text(surface, "id")
        if surface_id in surface_ids:
            raise ValueError(f"duplicate surface id: {surface_id}")
        surface_ids.add(surface_id)
        for field in ("title", "family", "audience", "mission", "canonical_owner"):
            _required_text(surface, field)
        for contract_id in surface.get("contracts", []):
            if contract_id not in contracts:
                raise ValueError(
                    f"{surface_id} references unknown contract: {contract_id}"
                )
        for story in surface.get("stories", []):
            story_id = _required_text(story, "id")
            if story_id in story_ids:
                raise ValueError(f"duplicate story id: {story_id}")
            story_ids.add(story_id)
            _required_text(story, "title")
            _required_text(story, "state")

    for surface in surfaces:
        surface_id = surface["id"]
        for related_id in surface.get("related", []):
            if related_id not in surface_ids:
                raise ValueError(
                    f"{surface_id} references unknown related surface: {related_id}"
                )
        owner = surface["canonical_owner"]
        if owner not in surface_ids:
            raise ValueError(f"{surface_id} references unknown owner: {owner}")
    return document


def _required_text(value: dict[str, Any], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"catalog field {field!r} must be non-empty text")
    return result


def _render_stories(
    catalog: dict[str, Any], output: Path, selected_surface: str | None
) -> dict[str, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(PROJECT_ROOT))

    from PySide6.QtCore import QSignalBlocker, QTimer
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    from vntts.app import SettingsDialog, build_unknown_speaker_prompt
    from vntts.dashboard_ui import ControlDashboard, RuntimeControlState
    from vntts.game_narrator_ui import GameNarratorDialog
    from vntts.settings import AppSettings
    from vntts.voice_library import VoiceLibrary

    app = QApplication.instance() or QApplication(["vntts-ui-catalog"])
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#272727"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#151515"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#303030"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#555555"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f0f0f0"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#148a2b"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)

    settings = AppSettings(
        onboarding_completed=True,
        speech_backend="moss-tts",
        tts_profile="stable",
        game_window_title="Reverse: 1999",
    )
    screenshots = output / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    for screenshot in screenshots.glob("*.png"):
        screenshot.unlink()

    def dashboard_stories() -> Any:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.set_status("Ready. Choose a prepared story or start live reading.")
        dashboard.set_ready(True)
        dashboard.show_stories()
        return dashboard

    def dashboard_reading() -> Any:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.set_status("Reading the selected game window.")
        dashboard.set_dialogue(
            "Believer IV",
            "They at least paid with their lives. What about you?",
        )
        dashboard.set_runtime_controls(
            RuntimeControlState(
                ready=True,
                live=True,
                speaking=True,
                queued=True,
                replayable=True,
            )
        )
        dashboard.set_live(True)
        dashboard.set_speech_identity(settings, narrator="Centurion")
        dashboard.voice.setText("Believer IV voice")
        dashboard.audio_source.setText("Live TTS")
        dashboard.speech_runtime.setText("Compute: Apple GPU · model loaded")
        dashboard.moss_runtime_button.setText("Unload OpenMOSS")
        dashboard.moss_runtime_button.setEnabled(False)
        dashboard.show_reading()
        return dashboard

    def dashboard_setup() -> Any:
        dashboard = dashboard_stories()
        dashboard.setup_more_button.setChecked(True)
        return dashboard

    def dashboard_voice_saved() -> Any:
        dashboard = ControlDashboard(settings)
        dashboard.resize(1040, 760)
        dashboard.set_status(
            "Voice saved for Narrator: Believer IV. Prepared recordings are unchanged."
        )
        dashboard.set_speech_identity(settings, narrator="Believer IV")
        dashboard.set_ready(True)
        dashboard.show_voices()
        return dashboard

    def settings_speech() -> Any:
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        reference = Path(temporary.name) / "reference.wav"
        reference.touch()
        dialog = SettingsDialog(
            settings.updated(tts_speaker_wav=str(reference)),
            voice_library=VoiceLibrary(Path(temporary.name) / "voice-library"),
        )
        dialog._catalog_temporary_directory = temporary
        dialog.resize(980, 760)
        dialog.section_navigation.setCurrentIndex(2)
        return dialog

    def settings_validation_error() -> Any:
        dialog = settings_speech()
        dialog.advanced_narrator.setChecked(True)
        dialog.narrator_reference.setText("/missing/voice-reference.wav")
        dialog.validate_and_accept()
        return dialog

    def voice_editor(
        role: str,
        *,
        generating: bool = False,
        failed: bool = False,
        compact_long_values: bool = False,
    ) -> Any:
        dashboard = ControlDashboard(settings)
        dashboard.resize(
            820 if compact_long_values else 1180,
            720 if compact_long_values else 900,
        )
        importer = Mock()
        importer.selected_installation_root.return_value = None
        pool = Mock()
        pool.start.side_effect = lambda _task: None
        previews = Mock()
        previews.backend.runtime_status = "Apple GPU · model loaded"
        temporary = TemporaryDirectory(prefix="vntts-ui-catalog-")
        dashboard._catalog_temporary_directory = temporary
        panel = GameNarratorDialog(
            settings,
            importer=importer,
            preview_service=previews,
            thread_pool=pool,
            player=Mock(),
            voice_library=VoiceLibrary(Path(temporary.name) / "voice-library"),
        )
        panel._initializing = True
        long_role = "The Extremely Long Character Name Used to Verify Layout"
        selected_role = long_role if compact_long_values else role
        panel.set_voice_context(
            character=selected_role,
            roles=("Believer IV", "Centurion", "Selone", long_role),
            story_titles=("Chapter 7",),
        )
        selected_character = (
            long_role
            if compact_long_values
            else role
            if role != "Narrator"
            else "Believer IV"
        )
        if compact_long_values:
            reference_title = (
                "Chapter 7 - A deliberately long original reference title that "
                "must remain readable without hiding the playback action"
            )
            transcript = (
                "A deliberately long transcript verifies wrapping and resizing "
                "while remaining visible beside the reference it describes."
            )
        elif role == "Selone":
            reference_title = "Chapter 7 - Selone reference 1"
            transcript = "The road is quiet now. We should keep moving."
        else:
            reference_title = (
                "Chapter 7 - They at least paid with their lives. What about you?"
            )
            transcript = "They at least paid with their lives. What about you?"
        with QSignalBlocker(panel.characters), QSignalBlocker(panel.references):
            panel.characters.clear()
            panel.characters.addItems(("Believer IV", "Centurion", "Selone", long_role))
            panel.characters.setCurrentText(selected_character)
            panel.references.clear()
            panel.references.addItem(
                reference_title,
                f"reference:{selected_character.casefold().replace(' ', '-')}"
                ":chapter-7",
            )
        panel._character = panel.characters.currentText()
        with QSignalBlocker(panel.source):
            panel.source.setCurrentIndex(panel.source.findData("game"))
        panel.reference_text.setText(transcript)
        panel.reference_details.setText(
            f"{selected_character} spoken reference 1 | 12.510 s\n"
            "Technical reference checks passed; voice quality is yours to judge."
        )
        panel._prepared[panel.references.currentData()] = (
            Path(temporary.name) / "reference-manifest.json"
        )
        panel._initializing = False
        panel._engine_available = lambda: True
        panel._update()
        panel.status.setText(
            "Choose a voice for this role. Nothing changes until you save."
            if role == "Narrator"
            else f"{role} was not mapped during live reading. Choose and save a voice."
        )
        if role == "Selone":
            panel.set_recovery_context("Selone", resume_live=True)
        if generating:
            QTimer.singleShot(
                0,
                lambda: panel._start(
                    "preview", "Generating your preview...", lambda: None
                ),
            )
        elif failed:

            def finish_with_error() -> None:
                panel._operation = "preview"
                with patch("vntts.support.record_game_import"):
                    panel._finished(None, RuntimeError("Preview generation failed."))

            QTimer.singleShot(0, finish_with_error)
        dashboard.embed_narrator(panel)
        dashboard.set_status(
            "Choose and preview a voice. Nothing changes until you save."
        )
        return dashboard

    def unknown_speaker_prompt() -> Any:
        prompt, _choose, _continue, _cancel = build_unknown_speaker_prompt("Selone")
        return prompt

    renderers: dict[str, Callable[[], Any]] = {
        "dashboard.stories-ready": dashboard_stories,
        "dashboard.reading-active": dashboard_reading,
        "dashboard.setup-expanded": dashboard_setup,
        "settings.speech-and-voices": settings_speech,
        "settings.validation-error": settings_validation_error,
        "unknown-speaker-prompt.awaiting-choice": unknown_speaker_prompt,
        "voice-editor.narrator": lambda: voice_editor("Narrator"),
        "voice-editor.live-recovery": lambda: voice_editor("Selone"),
        "voice-editor.preview-generating": lambda: voice_editor(
            "Narrator", generating=True
        ),
        "voice-editor.preview-failure": lambda: voice_editor("Narrator", failed=True),
        "voice-editor.long-values": lambda: voice_editor(
            "Narrator", compact_long_values=True
        ),
        "voice-editor.saved-return": dashboard_voice_saved,
    }
    surfaces = {surface["id"]: surface for surface in catalog["surfaces"]}
    allowed_surfaces = set(surfaces)
    if selected_surface:
        if selected_surface not in surfaces:
            raise ValueError(f"unknown surface: {selected_surface}")
        target = surfaces[selected_surface]
        allowed_surfaces = {
            selected_surface,
            target["canonical_owner"],
            *target.get("related", []),
        }

    captured: dict[str, str] = {}
    for surface in catalog["surfaces"]:
        if surface["id"] not in allowed_surfaces:
            continue
        for story in surface.get("stories", []):
            story_id = story["id"]
            renderer = renderers.get(story_id)
            if renderer is None:
                continue
            widget = renderer()
            try:
                widget.show()
                app.processEvents()
                image_name = f"{story_id}.png"
                image_path = screenshots / image_name
                if not widget.grab().save(str(image_path)):
                    raise RuntimeError(f"could not save {image_path}")
                captured[story_id] = f"screenshots/{image_name}"
            finally:
                for timer in widget.findChildren(QTimer):
                    timer.stop()
                widget.close()
                widget.deleteLater()
                app.processEvents()
    return captured


def _review_packet(
    catalog: dict[str, Any], surface: dict[str, Any], captured: dict[str, str]
) -> dict[str, Any]:
    by_id = {item["id"]: item for item in catalog["surfaces"]}
    related_ids = list(
        dict.fromkeys([surface["canonical_owner"], *surface.get("related", [])])
    )

    def public_surface(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": value["id"],
            "title": value["title"],
            "family": value["family"],
            "audience": value["audience"],
            "mission": value["mission"],
            "canonical_owner": value["canonical_owner"],
            "related": list(value.get("related", [])),
            "contracts": [
                {"id": contract_id, "rule": catalog["contracts"][contract_id]}
                for contract_id in value.get("contracts", [])
            ],
            "stories": [
                {
                    "id": story["id"],
                    "title": story["title"],
                    "state": story["state"],
                    "screenshot": captured.get(story["id"]),
                }
                for story in value.get("stories", [])
            ],
        }

    return {
        "review_boundary": (
            "Review product behavior and visible interface only. Do not infer or "
            "request implementation details."
        ),
        "target": public_surface(surface),
        "related_surfaces": [
            public_surface(by_id[surface_id])
            for surface_id in related_ids
            if surface_id != surface["id"]
        ],
    }


def _write_catalog(
    catalog: dict[str, Any], output: Path, captured: dict[str, str]
) -> None:
    packets = output / "review-packets"
    packets.mkdir(parents=True, exist_ok=True)
    for surface in catalog["surfaces"]:
        packet = _review_packet(catalog, surface, captured)
        (packets / f"{surface['id']}.json").write_text(
            json.dumps(packet, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for surface in catalog["surfaces"]:
        groups[surface["family"]].append(surface)

    navigation: list[str] = []
    sections: list[str] = []
    for family, surfaces in groups.items():
        navigation.append(f"<h3>{html.escape(family)}</h3><ul>")
        sections.append(f"<section><h2>{html.escape(family)}</h2>")
        for surface in surfaces:
            surface_id = surface["id"]
            navigation.append(
                f'<li><a href="#{html.escape(surface_id)}">'
                f"{html.escape(surface['title'])}</a></li>"
            )
            owner = surface["canonical_owner"]
            relations = ", ".join(surface.get("related", [])) or "None"
            contracts = "".join(
                "<li><strong>"
                + html.escape(contract_id)
                + ":</strong> "
                + html.escape(catalog["contracts"][contract_id])
                + "</li>"
                for contract_id in surface.get("contracts", [])
            )
            stories = []
            for story in surface.get("stories", []):
                screenshot = captured.get(story["id"])
                image = (
                    f'<a href="{html.escape(screenshot)}"><img src="{html.escape(screenshot)}" '
                    f'alt="{html.escape(story["title"])}"></a>'
                    if screenshot
                    else '<div class="missing">Map only: deterministic render not added yet.</div>'
                )
                stories.append(
                    '<article class="story">'
                    f"<h4>{html.escape(story['title'])}</h4>"
                    f"<p>{html.escape(story['state'])}</p>{image}</article>"
                )
            if not stories:
                stories.append(
                    '<div class="missing">Mapped relationship; render when this surface is next changed.</div>'
                )
            sections.append(
                f'<article class="surface" id="{html.escape(surface_id)}">'
                f"<header><div><h3>{html.escape(surface['title'])}</h3>"
                f"<p>{html.escape(surface['mission'])}</p></div>"
                f'<a class="packet" href="review-packets/{html.escape(surface_id)}.json">Astra packet</a></header>'
                '<dl class="meta">'
                f"<dt>Audience</dt><dd>{html.escape(surface['audience'])}</dd>"
                f"<dt>Canonical owner</dt><dd>{html.escape(owner)}</dd>"
                f"<dt>Related</dt><dd>{html.escape(relations)}</dd></dl>"
                f'<ul class="contracts">{contracts}</ul>'
                f'<div class="stories">{"".join(stories)}</div></article>'
            )
        navigation.append("</ul>")
        sections.append("</section>")

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(catalog["title"])}</title>
<style>
:root {{ color-scheme: dark; font: 15px/1.5 system-ui, sans-serif; background:#171717; color:#eee; }}
* {{ box-sizing:border-box; }} body {{ margin:0; }} a {{ color:#8fcbff; }}
nav {{ position:fixed; inset:0 auto 0 0; width:270px; overflow:auto; padding:20px; background:#202020; border-right:1px solid #444; }}
nav h1 {{ font-size:18px; margin:0 0 20px; }} nav h3 {{ margin:18px 0 4px; color:#bbb; font-size:12px; text-transform:uppercase; }}
nav ul {{ list-style:none; margin:0; padding:0; }} nav li {{ margin:5px 0; }}
main {{ margin-left:270px; padding:28px; max-width:1500px; }} main > p {{ color:#bbb; max-width:850px; }}
section > h2 {{ margin-top:42px; border-bottom:1px solid #444; padding-bottom:8px; }}
.surface {{ background:#252525; border:1px solid #444; border-radius:12px; margin:18px 0; padding:20px; }}
.surface header {{ display:flex; gap:20px; align-items:start; justify-content:space-between; }} h3,h4,p {{ margin-top:0; }}
.packet {{ white-space:nowrap; border:1px solid #666; border-radius:7px; padding:6px 10px; text-decoration:none; }}
.meta {{ display:grid; grid-template-columns:max-content 1fr; gap:4px 14px; }} .meta dt {{ color:#aaa; }} .meta dd {{ margin:0; }}
.contracts {{ padding-left:20px; color:#ccc; }} .stories {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); gap:16px; margin-top:18px; }}
.story {{ background:#1b1b1b; border-radius:9px; padding:14px; }} .story img {{ display:block; width:100%; height:auto; border:1px solid #555; border-radius:6px; }}
.missing {{ color:#aaa; border:1px dashed #555; border-radius:7px; padding:12px; }}
@media (max-width:850px) {{ nav {{ position:static; width:auto; }} main {{ margin:0; padding:18px; }} .stories {{ grid-template-columns:1fr; }} }}
</style></head><body>
<nav><h1>{html.escape(catalog["title"])}</h1>{"".join(navigation)}</nav>
<main><h1>Interface catalog</h1><p>One product map, shared visual contracts and reproducible states of the real Qt widgets. Use each Astra packet with the target screenshots; it deliberately contains no source-code context.</p>{"".join(sections)}</main>
</body></html>"""
    (output / "index.html").write_text(page, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--surface", help="render a target and its mapped neighbours")
    parser.add_argument("--validate-only", action="store_true")
    arguments = parser.parse_args()

    catalog = load_catalog(arguments.catalog)
    if arguments.validate_only:
        print(f"Validated {len(catalog['surfaces'])} surfaces.")
        return 0
    arguments.output.mkdir(parents=True, exist_ok=True)
    captured = _render_stories(catalog, arguments.output, arguments.surface)
    _write_catalog(catalog, arguments.output, captured)
    print(
        f"Rendered {len(captured)} stories across {len(catalog['surfaces'])} mapped surfaces to {arguments.output / 'index.html'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
