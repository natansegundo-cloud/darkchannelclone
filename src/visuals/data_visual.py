"""Contrato estruturado minimo para visualizacoes de dados locais."""

from __future__ import annotations

import re
from typing import Any, Mapping


DATA_VISUAL_FIELDS = {
    "type",
    "count",
    "colors",
    "distribution",
    "point_radius",
}
SUPPORTED_DATA_VISUAL = "point_distribution"
SUPPORTED_DISTRIBUTION = "moderate"
HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


def validate_data_visual(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, Mapping):
        return ["data_visual must be an object"]
    if set(value) != DATA_VISUAL_FIELDS:
        errors.append("data_visual fields are not canonical")
    if value.get("type") != SUPPORTED_DATA_VISUAL:
        errors.append("data_visual type must be point_distribution")

    count = value.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        errors.append("data_visual count must be a positive integer")

    colors = value.get("colors")
    if not isinstance(colors, list) or not colors:
        errors.append("data_visual colors must be a non-empty list")
    else:
        invalid_colors = [
            color
            for color in colors
            if not isinstance(color, str) or not HEX_COLOR_PATTERN.fullmatch(color)
        ]
        if invalid_colors:
            errors.append("data_visual colors must use #RRGGBB hexadecimal format")
        if len(set(colors)) != len(colors):
            errors.append("data_visual colors must be unique")
        if isinstance(count, int) and not isinstance(count, bool) and count < len(colors):
            errors.append("data_visual count must use every declared color")

    if value.get("distribution") != SUPPORTED_DISTRIBUTION:
        errors.append("data_visual distribution must be moderate")
    radius = value.get("point_radius")
    if (
        isinstance(radius, bool)
        or not isinstance(radius, int)
        or not 1 <= radius <= 100
    ):
        errors.append("data_visual point_radius must be an integer from 1 to 100")
    return errors


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    if not HEX_COLOR_PATTERN.fullmatch(value):
        raise ValueError(f"invalid hexadecimal color: {value}")
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))
