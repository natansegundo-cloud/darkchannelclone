"""Roteamento visual centralizado por tipo de cena."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


ROUTING_SOURCE = "SCENE_TYPE"
GENERATIVE_RENDERER = "generative"
DETERMINISTIC_LOCAL_RENDERER = "deterministic_local"


@dataclass(frozen=True)
class VisualRoute:
    renderer: str
    provider: str
    model: str
    primary_tier: str | None
    allow_fallback: bool
    api_required: bool
    cost_usd: float


def scene_declares_fin(scene: Mapping[str, Any]) -> bool:
    """Determina presenca de FIN somente pelos campos narrativos do JSON."""

    values: list[str] = []
    for field in (
        "dominant_idea",
        "situation",
        "action",
        "expression",
        "environment",
        "props",
    ):
        value = scene[field]
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        else:
            values.append(str(value))
    return any(re.search(r"\bFIN\b", value) for value in values)


def resolve_visual_route(
    scene: Mapping[str, Any],
    config: Mapping[str, Any],
) -> VisualRoute:
    """Resolve renderer/provider sem qualquer excecao por identificador de cena."""

    routing = config["scene_type_routing"]
    if routing["source"] != ROUTING_SOURCE:
        raise ValueError("visual routing source must be SCENE_TYPE")
    routes = routing["routes"]
    scene_type = str(scene["scene_type"])
    definition = routes[scene_type]
    if scene_type == "OBJECT_SCENE" and scene_declares_fin(scene):
        definition = routes[str(definition["with_character_route"])]

    renderer = str(definition["renderer"])
    if renderer == DETERMINISTIC_LOCAL_RENDERER:
        return VisualRoute(
            renderer=renderer,
            provider=str(definition["provider"]),
            model=str(definition["model"]),
            primary_tier=None,
            allow_fallback=False,
            api_required=False,
            cost_usd=float(definition["cost_usd"]),
        )
    if renderer != GENERATIVE_RENDERER:
        raise ValueError(f"unsupported visual renderer: {renderer}")
    tier_name = str(definition["tier"])
    tier = config["tiers"][tier_name]
    return VisualRoute(
        renderer=renderer,
        provider=str(tier["provider"]),
        model=str(tier["model"]),
        primary_tier=tier_name,
        allow_fallback=bool(definition["allow_fallback"]),
        api_required=True,
        cost_usd=float(tier["estimated_cost_per_image"]),
    )


def routed_tier_names(
    route: VisualRoute,
    config: Mapping[str, Any],
    *,
    enable_premium: bool,
    allowed_tiers: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Retorna apenas tiers permitidos pela rota e pela restricao explicita do caller."""

    if not route.api_required or route.primary_tier is None:
        return ()
    policy = config["fallback_policy"]
    ordered = (
        str(policy["first_tier"]),
        str(policy["second_tier"]),
        str(policy["third_tier"]),
    )
    if route.primary_tier not in ordered:
        raise ValueError(f"route tier is absent from fallback policy: {route.primary_tier}")
    start = ordered.index(route.primary_tier)
    candidates = ordered[start:] if route.allow_fallback else (route.primary_tier,)
    explicitly_allowed = set(allowed_tiers) if allowed_tiers is not None else None
    selected: list[str] = []
    for tier_name in candidates:
        if explicitly_allowed is not None and tier_name not in explicitly_allowed:
            continue
        tier = config["tiers"][tier_name]
        enabled = bool(tier["enabled"])
        if tier_name == "premium_optional" and enable_premium:
            enabled = True
        if enabled:
            selected.append(tier_name)
    return tuple(selected)
