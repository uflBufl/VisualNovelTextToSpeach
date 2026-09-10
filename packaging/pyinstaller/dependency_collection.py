from PyInstaller.utils.hooks import collect_all, copy_metadata


def collect_packaged_dependencies(datas, binaries, hidden_imports):
    provider_datas, provider_binaries, provider_imports = collect_all("r1999extractor")
    datas.extend(provider_datas)
    binaries.extend(provider_binaries)
    hidden_imports.extend(provider_imports)

    for package in ("TTS", "coqpit", "gruut", "ko_speech_tools", "trainer"):
        package_datas, package_binaries, package_imports = collect_all(package)
        datas.extend(package_datas)
        binaries.extend(package_binaries)
        hidden_imports.extend(package_imports)

    for distribution in (
        "coqui-tts",
        "coqpit",
        "gruut",
        "ko-speech-tools",
        "torch",
        "torchaudio",
        "torchcodec",
        "trainer",
        "transformers",
        "reverse1999-extractor",
    ):
        try:
            datas.extend(copy_metadata(distribution))
        except Exception:
            pass
