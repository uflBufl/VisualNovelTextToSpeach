import argparse
import json
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

project_root = Path(__file__).resolve().parents[1]
default_output = project_root / "data" / "reverse1999-voices"
wiki_api = "https://reverse1999.fandom.com/api.php"
user_agent = "VisualNovelTextToSpeech/0.1 voice-reference provisioner"
playable_categories = [f"Category:{rarity}-Star Crew Members" for rarity in range(2, 7)]
npc_category = "Category:NPCs"
preferred_recordings = [
    "first_encounter",
    "chitchat_1",
    "chitchat_2",
    "monologue",
    "hobby",
    "praise",
    "intimacy",
    "morning",
    "night",
    "greetings",
]
default_reference_count = 3
default_workers = 4
max_reference_attempts = 15


class WikiAPIError(RuntimeError):
    pass


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Download several local Reverse: 1999 voice references per character "
            "and generate a VNTTS voice manifest."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help="Directory for the manifest and downloaded references.",
    )
    parser.add_argument(
        "--playable-only",
        action="store_true",
        help="Exclude NPC pages from discovery.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Provision only the first N discovered characters for testing.",
    )
    parser.add_argument(
        "--references",
        type=int,
        default=default_reference_count,
        help="Maximum varied references to download per character.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay between character requests in seconds.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=default_workers,
        help="Maximum characters to provision concurrently.",
    )
    return parser.parse_args()


def api_request(**parameters):
    parameters.update({"format": "json", "formatversion": 2})
    request = Request(
        f"{wiki_api}?{urlencode(parameters)}",
        headers={"User-Agent": user_agent},
    )
    last_error = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
            if "error" in result:
                raise WikiAPIError(result["error"].get("info", str(result["error"])))
            return result
        except (HTTPError, URLError, TimeoutError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2**attempt)
    raise WikiAPIError(str(last_error)) from last_error


def category_members(category):
    members = []
    continuation = None
    while True:
        parameters = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": category,
            "cmnamespace": 0,
            "cmlimit": "max",
        }
        if continuation:
            parameters["cmcontinue"] = continuation
        response = api_request(**parameters)
        members.extend(
            member["title"] for member in response["query"]["categorymembers"]
        )
        continuation = response.get("continue", {}).get("cmcontinue")
        if continuation is None:
            return members


def discover_characters(include_npcs):
    categories = list(playable_categories)
    if include_npcs:
        categories.append(npc_category)

    characters = set()
    for category in categories:
        characters.update(category_members(category))
    characters.discard("Sandbox")
    return sorted(characters, key=str.casefold)


def voice_line_images(character):
    response = api_request(
        action="parse",
        page=f"{character}/Voicelines",
        prop="images",
    )
    return [
        image
        for image in response["parse"].get("images", [])
        if image.casefold().endswith((".ogg", ".wav"))
        and "garment" not in image.casefold()
    ]


def order_references(images):
    def reference_priority(image):
        normalized_image = image.casefold().replace(" ", "_")
        for priority, preferred_recording in enumerate(preferred_recordings):
            if f"_{preferred_recording}." in normalized_image:
                return priority, normalized_image
        return len(preferred_recordings), normalized_image

    return sorted(images, key=reference_priority)


def resolve_media(image):
    response = api_request(
        action="query",
        titles=f"File:{image}",
        prop="imageinfo",
        iiprop="url|size|mime",
    )
    pages = response["query"]["pages"]
    if not pages or "imageinfo" not in pages[0]:
        raise WikiAPIError(f"No media URL for {image}")
    return pages[0]["imageinfo"][0]


def download(url, output):
    if output.is_file():
        return
    request = Request(url, headers={"User-Agent": user_agent})
    temporary_output = output.with_suffix(f"{output.suffix}.part")
    with urlopen(request, timeout=60) as response:
        temporary_output.write_bytes(response.read())
    temporary_output.replace(output)


def slugify(value):
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    )
    slug = "-".join(
        part
        for part in "".join(
            character if character.isalnum() else " " for character in ascii_value
        )
        .casefold()
        .split()
    )
    return slug or "character"


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def provision_character(character, references_directory, reference_count):
    images = voice_line_images(character)
    if not images:
        raise WikiAPIError("no archived voice recordings")

    slug = slugify(character)
    last_error = None
    references = []
    sources = []
    for reference_image in order_references(images)[:max_reference_attempts]:
        try:
            media = resolve_media(reference_image)
            if not str(media.get("mime", "")).casefold().startswith("audio/"):
                last_error = WikiAPIError(
                    f"{reference_image} is {media.get('mime') or 'not audio'}"
                )
                continue
            extension = Path(reference_image).suffix.casefold()
            reference_path = references_directory / (
                f"{slug}-{len(references) + 1:02d}{extension}"
            )
            download(media["url"], reference_path)
            references.append(f"references/{reference_path.name}")
            sources.append(media["descriptionurl"])
            if len(references) == reference_count:
                break
        except (WikiAPIError, HTTPError, URLError, TimeoutError) as error:
            last_error = error

    if not references:
        raise WikiAPIError(f"no downloadable voice recordings ({last_error})")
    return {
        "character": character,
        "speaker": f"reverse-1999-{slug}-v2",
        "references": references,
        "aliases": [],
        "sources": sources,
    }


def main():
    arguments = parse_arguments()
    if arguments.limit is not None and arguments.limit <= 0:
        raise ValueError("--limit must be positive")
    if arguments.references <= 0:
        raise ValueError("--references must be positive")
    if arguments.workers <= 0:
        raise ValueError("--workers must be positive")
    if arguments.delay < 0:
        raise ValueError("--delay cannot be negative")

    output_directory = arguments.output.expanduser().resolve()
    references_directory = output_directory / "references"
    references_directory.mkdir(parents=True, exist_ok=True)
    manifest_path = output_directory / "manifest.json"
    skipped_path = output_directory / "skipped.json"

    print("Discovering Reverse: 1999 characters...")
    characters = discover_characters(include_npcs=not arguments.playable_only)
    if arguments.limit is not None:
        characters = characters[: arguments.limit]
    print(f"Found {len(characters)} character pages")

    existing_manifest = read_json(manifest_path, {})
    can_resume = (
        existing_manifest.get("version") == 2
        and existing_manifest.get("reference_count", arguments.references)
        == arguments.references
    )
    voices = existing_manifest.get("voices", []) if can_resume else []
    existing_skipped = read_json(skipped_path, {}).get("characters", [])
    skipped = existing_skipped if can_resume else []
    completed_characters = {
        entry["character"] for entry in [*voices, *skipped] if "character" in entry
    }
    pending_characters = [
        character for character in characters if character not in completed_characters
    ]
    if completed_characters:
        print(f"Resuming after {len(completed_characters)} completed characters")

    def provision(character):
        try:
            voice = provision_character(
                character,
                references_directory,
                arguments.references,
            )
            return voice, None
        except (WikiAPIError, HTTPError, URLError, TimeoutError) as error:
            return None, {"character": character, "reason": str(error)}
        finally:
            if arguments.delay:
                time.sleep(arguments.delay)

    with ThreadPoolExecutor(max_workers=arguments.workers) as executor:
        future_characters = {
            executor.submit(provision, character): character
            for character in pending_characters
        }
        for future in as_completed(future_characters):
            character = future_characters[future]
            voice, skipped_character = future.result()
            if voice is not None:
                voices.append(voice)
                result = f"{len(voice['references'])} references"
            else:
                skipped.append(skipped_character)
                result = f"skipped ({skipped_character['reason']})"

            completed_characters.add(character)
            voices.sort(key=lambda entry: entry["character"].casefold())
            skipped.sort(key=lambda entry: entry["character"].casefold())
            print(
                f"[{len(completed_characters)}/{len(characters)}] {character}: {result}"
            )
            write_json(
                manifest_path,
                {
                    "version": 2,
                    "reference_count": arguments.references,
                    "voices": voices,
                },
            )
            write_json(skipped_path, {"characters": skipped})

    print(f"Provisioned {len(voices)} voices in {output_directory}")
    print(f"Skipped {len(skipped)} characters; details are in {skipped_path}")
    print("Downloaded audio is for local, non-commercial use and is ignored by Git.")


if __name__ == "__main__":
    main()
