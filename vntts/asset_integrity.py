asset_manifest_name = "vntts-asset.json"


class AssetError(RuntimeError):
    pass


class ModelIntegrityError(AssetError):
    pass
