from vntts.reverse1999_catalog import normalize_name

voice_aliases = {
    "Brimley": ("Slouch Hat",),
}


def aliases_for_character(character):
    normalized = normalize_name(character)
    for canonical_name, aliases in voice_aliases.items():
        if normalize_name(canonical_name) == normalized:
            return aliases
    return ()


def canonical_voice_name(value):
    normalized = normalize_name(value)
    for canonical_name, aliases in voice_aliases.items():
        known_names = (canonical_name, *aliases)
        if any(normalize_name(name) == normalized for name in known_names):
            return canonical_name
    return None
