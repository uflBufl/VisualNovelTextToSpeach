"""Validated interface map and product-facing review output, without Qt fixtures."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path
from typing import TypedDict


class CatalogStory(TypedDict):
    id: str
    title: str
    state: str


class CatalogSurface(TypedDict):
    id: str
    title: str
    family: str
    audience: str
    mission: str
    canonical_owner: str
    related: list[str]
    contracts: list[str]
    stories: list[CatalogStory]


class UICatalog(TypedDict):
    schema_version: int
    title: str
    contracts: dict[str, str]
    surfaces: list[CatalogSurface]


class PublicStory(CatalogStory):
    screenshot: str | None


class PublicContract(TypedDict):
    id: str
    rule: str


class PublicSurface(TypedDict):
    id: str
    title: str
    family: str
    audience: str
    mission: str
    canonical_owner: str
    related: list[str]
    contracts: list[PublicContract]
    stories: list[PublicStory]


class ReviewPacket(TypedDict):
    review_boundary: str
    target: PublicSurface
    related_surfaces: list[PublicSurface]


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"catalog {label} must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"catalog {label} keys must be text")
        result[key] = item
    return result


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"catalog {label} must be an array")
    return list(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"catalog field {label!r} must be non-empty text")
    return value


def _id(value: object, label: str) -> str:
    result = _text(value, label)
    if any(character in result for character in "/\\\0"):
        raise ValueError(f"catalog {label} must be a file-name identifier")
    return result


def _text_array(value: object, label: str) -> list[str]:
    return [_text(item, label) for item in _array(value, label)]


def _story(value: object, story_ids: set[str]) -> CatalogStory:
    record = _object(value, "story")
    story_id = _id(record.get("id"), "story id")
    if story_id in story_ids:
        raise ValueError(f"duplicate story id: {story_id}")
    story_ids.add(story_id)
    return {
        "id": story_id,
        "title": _text(record.get("title"), "story title"),
        "state": _text(record.get("state"), "story state"),
    }


def _surface(value: object, story_ids: set[str]) -> CatalogSurface:
    record = _object(value, "surface")
    return {
        "id": _id(record.get("id"), "surface id"),
        "title": _text(record.get("title"), "surface title"),
        "family": _text(record.get("family"), "surface family"),
        "audience": _text(record.get("audience"), "surface audience"),
        "mission": _text(record.get("mission"), "surface mission"),
        "canonical_owner": _text(record.get("canonical_owner"), "canonical_owner"),
        "related": _text_array(record.get("related", []), "related"),
        "contracts": _text_array(record.get("contracts", []), "contracts"),
        "stories": [
            _story(item, story_ids)
            for item in _array(record.get("stories", []), "stories")
        ],
    }


def load_catalog(path: Path) -> UICatalog:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    document = _object(raw, "root")
    version = document.get("schema_version")
    if type(version) is not int or version != 1:
        raise ValueError("ui-catalog.json must use schema_version 1")
    contracts = {
        key: _text(value, f"contract {key}")
        for key, value in _object(document.get("contracts"), "contracts").items()
    }
    story_ids: set[str] = set()
    surfaces = [
        _surface(value, story_ids)
        for value in _array(document.get("surfaces"), "surfaces")
    ]
    _validate_surface_links(surfaces, contracts)
    return {
        "schema_version": version,
        "title": _text(document.get("title"), "title"),
        "contracts": contracts,
        "surfaces": surfaces,
    }


def _validate_surface_links(
    surfaces: list[CatalogSurface], contracts: dict[str, str]
) -> None:
    surface_ids: set[str] = set()
    for surface in surfaces:
        surface_id = surface["id"]
        if surface_id in surface_ids:
            raise ValueError(f"duplicate surface id: {surface_id}")
        surface_ids.add(surface_id)
        for contract_id in surface["contracts"]:
            if contract_id not in contracts:
                raise ValueError(
                    f"{surface_id} references unknown contract: {contract_id}"
                )
    for surface in surfaces:
        surface_id = surface["id"]
        for related_id in surface["related"]:
            if related_id not in surface_ids:
                raise ValueError(
                    f"{surface_id} references unknown related surface: {related_id}"
                )
        owner = surface["canonical_owner"]
        if owner not in surface_ids:
            raise ValueError(f"{surface_id} references unknown owner: {owner}")


def _review_packet(
    catalog: UICatalog, surface: CatalogSurface, captured: dict[str, str]
) -> ReviewPacket:
    by_id = {item["id"]: item for item in catalog["surfaces"]}
    related_ids = list(dict.fromkeys([surface["canonical_owner"], *surface["related"]]))

    def public_surface(value: CatalogSurface) -> PublicSurface:
        return {
            "id": value["id"],
            "title": value["title"],
            "family": value["family"],
            "audience": value["audience"],
            "mission": value["mission"],
            "canonical_owner": value["canonical_owner"],
            "related": list(value["related"]),
            "contracts": [
                {"id": contract_id, "rule": catalog["contracts"][contract_id]}
                for contract_id in value["contracts"]
            ],
            "stories": [
                {
                    "id": story["id"],
                    "title": story["title"],
                    "state": story["state"],
                    "screenshot": captured.get(story["id"]),
                }
                for story in value["stories"]
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


def _write_catalog(catalog: UICatalog, output: Path, captured: dict[str, str]) -> None:
    packets = output / "review-packets"
    packets.mkdir(parents=True, exist_ok=True)
    for surface in catalog["surfaces"]:
        packet = _review_packet(catalog, surface, captured)
        (packets / f"{surface['id']}.json").write_text(
            json.dumps(packet, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    groups: dict[str, list[CatalogSurface]] = defaultdict(list)
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
            relations = ", ".join(surface["related"]) or "None"
            contracts = "".join(
                "<li><strong>"
                + html.escape(contract_id)
                + ":</strong> "
                + html.escape(catalog["contracts"][contract_id])
                + "</li>"
                for contract_id in surface["contracts"]
            )
            stories = []
            for story in surface["stories"]:
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
