"""Keep Pocket capability tests independent of locally downloaded models."""

from contextlib import ExitStack, contextmanager
from unittest.mock import Mock, patch


@contextmanager
def pocket_model_readiness(ready: bool):
    capability = Mock(return_value=ready)
    with ExitStack() as stack:
        for module in (
            "controller",
            "controller_components",
            "dashboard_ui",
            "game_narrator",
            "game_narrator_ui",
            "pocket_model",
            "pocket_setup_ui",
            "pregeneration_audition",
            "pregeneration_ui",
            "pregeneration_voices",
            "speech_presentation",
        ):
            stack.enter_context(patch(f"vntts.{module}.pocket_model_ready", capability))
        yield capability
