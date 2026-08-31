"""Application data locations without importing desktop input dependencies."""

import sys

from platformdirs import user_config_path, user_data_path

application_directory_name = "VisualNovelTextToSpeech"


def _platform_app_name():
    return (
        application_directory_name if sys.platform in {"darwin", "win32"} else "vntts"
    )


def get_config_directory():
    return user_config_path(_platform_app_name(), appauthor=False, roaming=True)


def get_local_data_directory():
    return user_data_path(_platform_app_name(), appauthor=False)
