"""Compila cenas JSON em prompts autocontidos, sem persistir artefatos de texto."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHARACTER_LOCK = ROOT / "config" / "character_fin.json"
DEFAULT_REFERENCE_PROFILE = ROOT / "config" / "visual_reference_profile.json"

SCENE_TYPES = (
    "CHARACTER_SCENE",
    "OBJECT_SCENE",
    "ENVIRONMENT_SCENE",
    "SIMPLE_DATA_SCENE",
)
TEXT_POLICY_MODES = ("NONE", "OVERLAY")
CHARACTER_PRESENCES = ("FIN", "NONE")

SECTION_ORDER = (
    "SCENE TYPE",
    "CONCRETE SITUATION",
    "ACTION",
    "EXPRESSION",
    "ENVIRONMENT",
    "PROPS",
    "FRAMING",
    "SUBJECT COUNT",
    "CHARACTER CONSISTENCY",
    "LIGHTING",
    "STYLE",
    "CONTINUITY",
    "VISUAL REFERENCE",
    "VISUAL RULESET",
    "SCENE SIMPLIFICATION RULES",
    "BEHAVIOR RULE",
    "COMPOSITION PRIORITY",
    "READABILITY RULE",
    "TEXT POLICY",
    "CHARACTER EMPHASIS RULE",
    "BACKGROUND SUBORDINATION RULE",
    "PROP LIMIT RULE",
    "ANTI-STAGING RULE",
    "CAMERA SIMPLICITY RULE",
    "NEGATIVE RULES",
)

SUBJECT_COUNT_ONE_FIN = (
    "Exactly one FIN character. No FIN clones. No duplicate FIN character. No duplicate character. "
    "No clone. No second version of FIN. No mirrored duplicate unless explicitly required by scene "
    "spec. Secondary people must not share FIN identity. Secondary people must not use a lime tie "
    "or FIN signature outfit unless explicitly required by scene spec. Other non-FIN people are "
    "allowed only when explicitly required by the scene spec."
)
NO_CHARACTER_RULE = (
    "NO CHARACTERS. NO PEOPLE. NO FIN. Do not depict or imply any person, human figure, "
    "face, body, mascot, or character."
)
ILLUSTRATED_STYLE_LOCK = (
    "ILLUSTRATED_V1 STYLE LOCK. Create a clean illustrated 2D visual in the same visual "
    "universe as FIN scenes, using clean editorial illustration. Apply this style with equal "
    "strength to every scene type, whether or not FIN is present. NOT PHOTOREALISTIC. "
    "NO PHOTOGRAPHY. NO 3D RENDER. NO PRODUCT PHOTOGRAPHY. NO CINEMATIC PHOTOGRAPH."
)
OBJECT_SCENE_GUARD = (
    "OBJECT_SCENE GUARD. Render only the declared objects, action, and environment as a clean "
    "2D editorial illustration. No photorealism, realistic 3D, stock-photo aesthetic, product "
    "photography, or cinematic photography."
)
DATA_SCENE_GUARD = (
    "SIMPLE_DATA_SCENE GUARD. Create a clean editorial data visualization in the same "
    "ILLUSTRATED_V1 visual universe. Render only the declared simple restrained point distribution "
    "and its declared colors and arrangement. No axes or labels unless declared. No character, "
    "no FIN, no people, no mascot, no celebration, no confetti, and no decorative narrative. "
    "No photorealism and no 3D."
)
CHARACTER_BEHAVIOR_RULE = (
    "The image must feel like a captured moment from everyday life, not a posed "
    "character showcase. Treat the declared ACTION and EXPRESSION as mandatory instructions; "
    "do not reinterpret, weaken, replace, or omit their semantic meaning. Prioritize situation, "
    "action clarity, and narrative context."
)
CHARACTER_COMPOSITION_PRIORITY = (
    "Keep the declared dominant idea, framing, subject side, important scale, environment, and "
    "one dominant action unchanged. The model may decide only fine spatial placement, natural pose "
    "within the declared action, minor perspective variation, shadows, fine lighting, clothing folds, "
    "and other non-narrative rendering details."
)
READABILITY_RULE = (
    "The declared main idea and essential visual elements must be understandable at a glance."
)
CHARACTER_EMPHASIS_RULE = (
    "FIN must feel integrated into the situation, not showcased as a decorative subject. "
    "Prioritize the everyday action over character display."
)
BACKGROUND_SUBORDINATION_RULE = (
    "Keep the environment supportive and secondary, with only the detail needed to make "
    "the situation immediately clear."
)
COMMON_PROP_RULE = (
    "Include every declared prop and do not remove essential props. Do not introduce undeclared "
    "narrative elements or additional narrative props."
)
ANTI_STAGING_RULE = (
    "Do not create a posed, promotional, decorative, or character-showcase composition. "
    "Show a natural captured moment from everyday life."
)
CAMERA_SIMPLICITY_RULE = (
    "Prefer straightforward framing, a clear silhouette, and one dominant readable action. "
    "Avoid polished set dressing and visually busy interiors."
)
VISUAL_RULESET = (
    "SYSTEM DECIDES CONTENT. AI DECIDES APPEARANCE. Dominant idea, situation, action, expression, "
    "environment, declared props, framing, lighting, continuity, declared character presence, and text policy "
    "are fixed content decisions. The model decides only their fine visual rendering. Do not introduce "
    "undeclared narrative elements or additional narrative props."
)
FIN_PRESENCE_RULESET = (
    "FIN is declared by the scene JSON and must use FIN_V1. FIN normally occupies 25% to 40% of "
    "the frame, with a natural functional pose and an expression serving the situation."
)
NO_UNDECLARED_TEXT_RULE = (
    "NO UNDECLARED TEXT. No readable text, no letters, no numbers, no logos, no pseudo-text, "
    "and no gibberish typography. No random numbers, invented labels, pseudo-words, gibberish, "
    "or floating typography anywhere."
)
TEXT_BEARING_OBJECT_RULE = (
    "If a calendar, clipboard, document, receipt, menu, sign, screen, book, newspaper, label, "
    "poster, or any other normally text-bearing object is present, keep the object in the scene "
    "but represent its content only with blank or abstract graphical markings, non-readable lines "
    "and shapes, and no legible characters."
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


@lru_cache(maxsize=1)
def _character_lock() -> dict[str, Any]:
    return _load_json(DEFAULT_CHARACTER_LOCK)


@lru_cache(maxsize=1)
def _visual_profile() -> dict[str, Any]:
    return _load_json(DEFAULT_REFERENCE_PROFILE)


def _normalize(value: Any) -> str:
    return " ".join(str(value).split())


def _essential_character_description(character_lock: Mapping[str, Any]) -> str:
    traits = ("face", "hair", "clothing", "tie", "proportions", "palette", "outline")
    values: list[str] = []
    for trait in traits:
        value = character_lock[trait]
        if isinstance(value, Mapping):
            value = ", ".join(f"{key}: {item}" for key, item in value.items())
        values.append(f"{trait.replace('_', ' ')}: {value}")
    return "; ".join(values)


def _character_presence(scene: Mapping[str, Any]) -> str:
    """Resolve presenca de personagem somente pelo campo canonico do JSON."""

    presence = str(scene["character_presence"])
    if presence not in CHARACTER_PRESENCES:
        raise ValueError(f"unsupported character presence: {presence}")
    return presence


def _character_consistency(
    scene: Mapping[str, Any],
    character_lock: Mapping[str, Any],
    has_fin: bool,
) -> str:
    lock_id = character_lock["character_lock_version"]
    references = ", ".join(character_lock["canonical_references"])
    if not has_fin:
        return (
            f"{NO_CHARACTER_RULE} character_presence is NONE in the scene JSON. Preserve "
            "the official illustrated visual universe without importing character identity or anatomy."
        )
    return (
        f"Canonical character lock: {lock_id}. Preserve the exact same FIN identity and do "
        f"not redesign the character. Official references: {references}. Essential traits: "
        f"{_essential_character_description(character_lock)}."
    )


def _framing(scene: Mapping[str, Any], has_fin: bool) -> str:
    framing = scene["framing"]
    text = (
        f"{framing['shot']} shot, subject {framing['subject_position']}, "
        f"camera angle {framing['camera_angle']}"
    )
    occupancy = framing.get("fin_occupancy")
    if has_fin and occupancy is not None:
        text += f", FIN occupies about {round(float(occupancy) * 100)}% of the frame"
    return text + ". Horizontal 16:9, 1920x1080."


def _text_policy(scene: Mapping[str, Any]) -> str:
    policy = scene["text_policy"]
    mode = str(policy["mode"])
    items = list(policy.get("items", []))
    if mode not in TEXT_POLICY_MODES:
        raise ValueError(f"unsupported text policy mode: {mode}")
    if mode == "NONE":
        return (
            "NONE. No narratively necessary text exists in this scene. Render no visible text, no letters, "
            f"no numbers, no logos, and no labels. {NO_UNDECLARED_TEXT_RULE} "
            f"{TEXT_BEARING_OBJECT_RULE}"
        )
    targets = ", ".join(str(item["target"]) for item in items) or "the declared target surfaces"
    return (
        "OVERLAY. Declared text and numbers are structured metadata only. Do not render their content. "
        f"Generate clean blank surfaces reserved for later deterministic text overlay on {targets}. "
        "Use neutral visual placeholders only. Exact text is applied deterministically after image generation. "
        f"{NO_UNDECLARED_TEXT_RULE}"
    )


def _text_policy_mode(scene: Mapping[str, Any]) -> str:
    policy = scene["text_policy"]
    mode = str(policy["mode"])
    if mode not in TEXT_POLICY_MODES:
        raise ValueError(f"unsupported text policy mode: {mode}")
    return mode


def _style_rules(
    profile: Mapping[str, Any],
    scene_type: str,
    has_fin: bool,
) -> str:
    if scene_type == "SIMPLE_DATA_SCENE":
        return (
            f"{ILLUSTRATED_STYLE_LOCK} Use a clean editorial data visualization with a simple "
            "restrained point distribution, minimal shading, and a clean off-white editorial finish."
        )
    rules = list(profile["style_rules"])
    if scene_type != "CHARACTER_SCENE":
        rules = [
            rule
            for rule in rules
            if "FIN" not in rule and "concrete everyday situation" not in rule
        ]
    return " ".join([ILLUSTRATED_STYLE_LOCK, *rules])


def _visual_reference(profile_id: str, has_fin: bool) -> str:
    if has_fin:
        return (
            f"Use canonical visual profile {profile_id}. Use the approved references as "
            "qualitative style and character guidance, never as fixed scene geometry."
        )
    return (
        f"Use canonical visual profile {profile_id}. Keep the same visual universe as FIN scenes "
        "through the approved palette, linework, editorial finish, and shading only. Character "
        f"reference imagery is style-only and cannot override this scene's absence of characters. {NO_CHARACTER_RULE}"
    )


def _scene_type_specific_rules(scene_type: str, has_fin: bool) -> dict[str, str]:
    if scene_type == "CHARACTER_SCENE":
        return {
            "BEHAVIOR RULE": f"{FIN_PRESENCE_RULESET} {CHARACTER_BEHAVIOR_RULE}",
            "COMPOSITION PRIORITY": CHARACTER_COMPOSITION_PRIORITY,
            "CHARACTER EMPHASIS RULE": CHARACTER_EMPHASIS_RULE,
            "BACKGROUND SUBORDINATION RULE": BACKGROUND_SUBORDINATION_RULE,
            "ANTI-STAGING RULE": ANTI_STAGING_RULE,
            "CAMERA SIMPLICITY RULE": CAMERA_SIMPLICITY_RULE,
        }
    if scene_type == "OBJECT_SCENE":
        rules = {
            "BEHAVIOR RULE": (
                f"{OBJECT_SCENE_GUARD} Static composition is allowed. Preserve the declared object "
                "arrangement and do not invent movement."
            ),
            "COMPOSITION PRIORITY": (
                "Keep the declared dominant idea, framing, environment, objects, and object arrangement "
                "unchanged. The model may decide only fine spatial placement, minor perspective variation, "
                "shadows, lighting, and surface rendering."
            ),
            "BACKGROUND SUBORDINATION RULE": BACKGROUND_SUBORDINATION_RULE,
        }
    elif scene_type == "SIMPLE_DATA_SCENE":
        return {
            "BEHAVIOR RULE": DATA_SCENE_GUARD,
            "COMPOSITION PRIORITY": (
                "Keep the declared point distribution, two-color treatment, framing, and background "
                "unchanged. The model may decide only fine point placement and non-narrative rendering details."
            ),
        }
    else:
        rules = {
            "BEHAVIOR RULE": (
                "ENVIRONMENT_SCENE GUARD. Keep the declared environment dominant and render only the "
                "subjects and activity explicitly declared by the scene JSON."
            ),
            "COMPOSITION PRIORITY": (
                "Keep the declared dominant idea, framing, environment, declared subjects, and props unchanged. "
                "The model may decide only fine spatial placement, perspective, lighting, and surface rendering."
            ),
        }
    if has_fin:
        rules["BEHAVIOR RULE"] = (
            f"{FIN_PRESENCE_RULESET} {CHARACTER_BEHAVIOR_RULE} {rules['BEHAVIOR RULE']}"
        )
        rules["CHARACTER EMPHASIS RULE"] = CHARACTER_EMPHASIS_RULE
        rules["ANTI-STAGING RULE"] = ANTI_STAGING_RULE
        rules["CAMERA SIMPLICITY RULE"] = CAMERA_SIMPLICITY_RULE
    return rules


def _negative_rules(
    profile: Mapping[str, Any],
    scene_type: str,
    has_fin: bool,
) -> str:
    profile_rules = list(profile["negative_rules"])
    if scene_type == "CHARACTER_SCENE":
        return " ".join(profile_rules)
    if scene_type == "SIMPLE_DATA_SCENE":
        return " ".join(
            [
                NO_CHARACTER_RULE,
                "No mascot, celebration, confetti, or decorative narrative.",
                "No axes or labels unless declared.",
                "No photorealism and no 3D.",
                "No text, letters, numbers, or watermarks unless declared by the text policy.",
            ]
        )
    if scene_type == "OBJECT_SCENE":
        rules = [
            rule
            for rule in profile_rules
            if "No text" in rule or "No extra people" in rule
        ]
        if not has_fin:
            rules.append(NO_CHARACTER_RULE)
        rules.append(OBJECT_SCENE_GUARD)
        return " ".join(rules)
    rules = [rule for rule in profile_rules if "promotional pose" not in rule]
    if not has_fin:
        rules = [rule for rule in rules if "FIN_V1" not in rule]
        rules.append(NO_CHARACTER_RULE)
    return " ".join(rules)


def _build_sections(scene: Mapping[str, Any]) -> dict[str, str]:
    character_lock = _character_lock()
    profile = _visual_profile()
    scene_type = str(scene["scene_type"])
    _text_policy_mode(scene)
    if scene_type not in SCENE_TYPES:
        raise ValueError(f"unsupported scene type: {scene_type}")
    character_presence = _character_presence(scene)
    if scene_type == "CHARACTER_SCENE" and character_presence != "FIN":
        raise ValueError("CHARACTER_SCENE requires character_presence FIN")
    if scene_type == "SIMPLE_DATA_SCENE" and character_presence != "NONE":
        raise ValueError("SIMPLE_DATA_SCENE requires character_presence NONE")
    has_fin = character_presence == "FIN"
    profile_id = str(profile["profile_id"])
    sections: dict[str, Any] = {
        "SCENE TYPE": scene_type,
        "CONCRETE SITUATION": f"{scene['dominant_idea']} {scene['situation']}",
        "ACTION": scene["action"],
        "EXPRESSION": scene["expression"],
        "ENVIRONMENT": scene["environment"],
        "PROPS": ", ".join(scene["props"]) if scene["props"] else "No declared narrative props.",
        "FRAMING": _framing(scene, has_fin),
        "SUBJECT COUNT": (
            SUBJECT_COUNT_ONE_FIN
            if scene_type == "CHARACTER_SCENE"
            else ("FIN presence follows the scene spec." if has_fin else NO_CHARACTER_RULE)
        ),
        "CHARACTER CONSISTENCY": _character_consistency(scene, character_lock, has_fin),
        "LIGHTING": scene["lighting"],
        "STYLE": _style_rules(profile, scene_type, has_fin),
        "CONTINUITY": scene["continuity_from"] or "Opening scene; no previous visual dependency.",
        "VISUAL REFERENCE": _visual_reference(profile_id, has_fin),
        "VISUAL RULESET": f"{VISUAL_RULESET} {NO_CHARACTER_RULE}" if not has_fin else VISUAL_RULESET,
        "SCENE SIMPLIFICATION RULES": COMMON_PROP_RULE,
        "READABILITY RULE": READABILITY_RULE,
        "TEXT POLICY": _text_policy(scene),
        "NEGATIVE RULES": _negative_rules(profile, scene_type, has_fin),
    }
    sections.update(_scene_type_specific_rules(scene_type, has_fin))
    return {
        name: _normalize(sections[name])
        for name in SECTION_ORDER
        if name in sections
    }


def build_source_prompt(scene: Mapping[str, Any]) -> str:
    """Compila a representação multilinha apenas para debug ou revisão humana."""

    return "\n\n".join(f"{name}\n{value}" for name, value in _build_sections(scene).items())


def build_prompt(scene: Mapping[str, Any]) -> str:
    """Compila o prompt canônico de provider em uma única linha, somente em memória."""

    prompt = " | ".join(f"{name}: {value}" for name, value in _build_sections(scene).items())
    if "\n" in prompt or "\r" in prompt:
        raise ValueError("compiled prompt must not contain CR or LF")
    return prompt
