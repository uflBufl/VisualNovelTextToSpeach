"""Select temporary text blocks from the same captured window as dialogue."""

from collections.abc import Callable
from dataclasses import replace

from vntts.chapter_voice_preload import StoryObservationMatch
from vntts.dialog_capture import CapturedDialogFrame
from vntts.ocr import OCRResult
from vntts.text_detection import ScreenTextDetection, center_in_region, group_text_lines


def select_caption(
    frame: CapturedDialogFrame,
    detection: ScreenTextDetection,
    minimum_confidence: float,
    *,
    story_matcher: Callable[[str], StoryObservationMatch] | None = None,
) -> CapturedDialogFrame:
    """Select among already permitted blocks; never concatenate ambiguous groups."""
    image = frame.canonical_image
    if image is None or detection.dialogue_text_present:
        return frame
    dialogue = frame.dialogue_text_region
    if dialogue is not None and any(
        line.text.strip() and center_in_region(line.box, dialogue)
        for line in detection.lines
    ):
        return frame
    candidates = group_text_lines(
        line
        for line in detection.lines
        if frame.capture_region is None
        or not center_in_region(line.box, frame.capture_region)
    )
    if not candidates:
        return frame
    if story_matcher is not None:
        matches = [(block, story_matcher(block.text)) for block in candidates]
        matches = [
            (block, match)
            for block, match in matches
            if match.method not in {"expected-no-match", "no-expected-candidates"}
        ]
        if not matches:
            return frame
        if (
            len(matches) != 1
            or matches[0][1].line is None
            or not matches[0][1].complete
        ):
            return replace(frame, layout_kind="pending", navigation_allowed=False)
        candidates = (matches[0][0],)
    if len(candidates) != 1:
        return replace(frame, layout_kind="pending", navigation_allowed=False)
    candidate = candidates[0]
    confidence = min(line.confidence for line in candidate.lines)
    if confidence < minimum_confidence:
        return replace(frame, layout_kind="pending", navigation_allowed=False)
    crop = candidate.box.crop(image)
    provenance = frame.provenance
    if provenance is not None:
        provenance = replace(
            provenance,
            capture_bounds=candidate.box.capture_box(
                {"width": image.width, "height": image.height}
            ),
            output_dimensions=crop.size,
        )
        crop.info["capture_provenance"] = provenance.as_dict()
    return replace(
        frame,
        image=crop,
        ocr_regions=None,
        layout_id="caption",
        layout_kind="caption",
        navigation_allowed=False,
        recognized_result=OCRResult(
            "Narrator",
            candidate.text,
            confidence,
            "caption-ppocr",
            1,
            processed_image=crop,
            dialogue_confidence=confidence,
        ),
        provenance=provenance,
    )
