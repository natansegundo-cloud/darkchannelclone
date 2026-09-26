"""Renderer raster local e deterministico para SIMPLE_DATA_SCENE."""

from __future__ import annotations

import json
import struct
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .data_visual import hex_to_rgb, validate_data_visual


RENDERER_ID = "deterministic_local"
RENDERER_VERSION = "2"
CANVAS_SIZE = (1920, 1080)
DEFAULT_CHARACTER_LOCK = Path(__file__).resolve().parents[2] / "config" / "character_fin.json"


@lru_cache(maxsize=1)
def official_background_rgb() -> tuple[int, int, int]:
    character_lock = json.loads(
        DEFAULT_CHARACTER_LOCK.read_text(encoding="utf-8-sig")
    )
    return hex_to_rgb(str(character_lock["palette"]["off_white"]))


def _halton(index: int, base: int) -> float:
    result = 0.0
    fraction = 1.0
    while index > 0:
        fraction /= base
        result += fraction * (index % base)
        index //= base
    return result


def _moderate_positions(count: int) -> tuple[tuple[float, float], ...]:
    return tuple(
        (
            0.27 + 0.46 * _halton(index, 2),
            0.31 + 0.38 * _halton(index, 3),
        )
        for index in range(1, count + 1)
    )


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def _draw_circle(
    pixels: bytearray,
    width: int,
    height: int,
    center_x: int,
    center_y: int,
    radius: int,
    color: tuple[int, int, int],
) -> None:
    radius_squared = radius * radius
    for y in range(max(0, center_y - radius), min(height, center_y + radius + 1)):
        y_offset = (y - center_y) ** 2
        for x in range(max(0, center_x - radius), min(width, center_x + radius + 1)):
            if (x - center_x) ** 2 + y_offset > radius_squared:
                continue
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(color)


def _encode_png(width: int, height: int, pixels: bytearray) -> bytes:
    stride = width * 3
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        start = y * stride
        scanlines.extend(pixels[start : start + stride])
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        (
            _png_chunk(b"IHDR", header),
            _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=9)),
            _png_chunk(b"IEND", b""),
        )
    )


def render_deterministic_scene(
    scene: Mapping[str, Any],
    output_path: Path,
) -> Path:
    """Renderiza somente a distribuicao de pontos declarada, sem texto ou API."""

    if scene.get("scene_type") != "SIMPLE_DATA_SCENE":
        raise ValueError("deterministic_local supports only SIMPLE_DATA_SCENE")
    text_policy = scene.get("text_policy", {})
    if text_policy.get("mode") != "NONE" or text_policy.get("items"):
        raise ValueError("deterministic_local requires text_policy NONE")
    data_visual = scene.get("data_visual")
    errors = validate_data_visual(data_visual)
    if errors:
        raise ValueError("; ".join(errors))
    assert isinstance(data_visual, Mapping)
    count = int(data_visual["count"])
    colors = tuple(hex_to_rgb(str(color)) for color in data_visual["colors"])
    point_radius = int(data_visual["point_radius"])
    distribution = str(data_visual["distribution"])
    if distribution != "moderate":
        raise ValueError(f"unsupported deterministic distribution: {distribution}")

    width, height = CANVAS_SIZE
    pixels = bytearray(bytes(official_background_rgb()) * (width * height))
    for index, (normalized_x, normalized_y) in enumerate(_moderate_positions(count)):
        _draw_circle(
            pixels,
            width,
            height,
            round(normalized_x * width),
            round(normalized_y * height),
            point_radius,
            colors[index % len(colors)],
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(_encode_png(width, height, pixels))
    return output_path
