"""VNTTS-side preflight checks for game-pack artifact bindings."""

from vntts_artifacts.game_pack import validate_game_pack_artifact_bindings

required_game_pack_artifacts = ("story_index", "voice_manifest")


def preflight_game_pack_checksums(pack_directory, artifact_bindings):
    """Reject missing or modified required artifacts before importing a pack."""
    return validate_game_pack_artifact_bindings(
        pack_directory,
        artifact_bindings,
        required=required_game_pack_artifacts,
    )
