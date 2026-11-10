"""Select temporary text blocks from the same captured window as dialogue."""

from dataclasses import replace

from vntts.dialog_capture import CapturedDialogFrame
from vntts.ocr import DialogRegion, OCRResult
from vntts.text_detection import TextBlock


def _inside(box: DialogRegion, region: DialogRegion) -> bool:
    x, y = box.left + box.width / 2, box.top + box.height / 2
    return (
        region.left <= x <= region.left + region.width
        and region.top <= y <= region.top + region.height
    )


def select_caption(
    frame: CapturedDialogFrame, blocks: tuple[TextBlock, ...], minimum_confidence: float
) -> CapturedDialogFrame:
    """Select among already permitted blocks; never concatenate ambiguous groups."""
    image = frame.canonical_image
    if image is None:
        return frame
    candidates = [
        block
        for block in blocks
        if any(
            frame.capture_region is None or not _inside(line.box, frame.capture_region)
            for line in block.lines
        )
    ]
    if not candidates:
        return frame
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
