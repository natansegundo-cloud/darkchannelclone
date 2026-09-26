"""Politica canonica de referencias enviadas a geracao visual."""

from __future__ import annotations

from typing import Any, Mapping


CHARACTER_PRESENCES = ("FIN", "NONE")


def resolve_scene_references(
    scene: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    include_scene_reference: bool = True,
) -> list[str]:
    """Resolve exatamente os arquivos permitidos pela presenca declarada."""

    character_presence = str(scene["character_presence"])
    if character_presence not in CHARACTER_PRESENCES:
        raise ValueError(f"unsupported character presence: {character_presence}")

    references: list[str] = []
    if character_presence == "FIN":
        references.extend(str(path) for path in profile.get("required_references", []))

    if include_scene_reference:
        scene_id = str(scene["scene_id"])
        scene_reference = profile.get("scene_reference_images", {}).get(scene_id)
        if scene_reference:
            if character_presence == "NONE":
                declared_presence = profile.get(
                    "scene_reference_character_presence", {}
                ).get(scene_id)
                if declared_presence != "NONE":
                    raise ValueError(
                        f"{scene_id}: scene reference requires explicit NONE compatibility"
                    )
            references.append(str(scene_reference))

    return list(dict.fromkeys(references))
