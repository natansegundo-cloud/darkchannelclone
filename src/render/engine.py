"""Deterministic, fail-closed orchestration for the final video render."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.episodes import EpisodeContext, resolve_active_episode

from .preflight import FRAME_RATE, ROOT, run_preflight


FINAL_RENDER_BLOCKED = "FINAL_RENDER_BLOCKED: episode is still in visual_qualification"
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
VIDEO_CODEC = "libx264"
AUDIO_CODEC = "aac"


class RenderError(RuntimeError):
    """Controlled render failure that is safe to expose in the CLI."""


class FinalRenderBlocked(RenderError):
    """The episode lifecycle does not permit a final render yet."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"could not read JSON: {path} ({exc})") from exc
    if not isinstance(value, dict):
        raise RenderError(f"JSON root must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _episode_stage(context: EpisodeContext) -> str | None:
    return context.production_stage


def build_render_plan(preflight: Mapping[str, Any]) -> dict[str, Any]:
    """Build the stable, JSON-serializable plan consumed by FFmpeg."""

    if not preflight.get("passed"):
        raise RenderError("cannot build render plan from a failed preflight")
    scenes = []
    for scene in preflight["scenes"]:
        scenes.append(
            {
                "scene_id": scene["scene_id"],
                "image_path": scene["image_source"],
                "audio_source": preflight["sources"]["narration"],
                "timing_source": preflight["sources"]["scene_map"],
                "start": round(float(scene["start"]), 6),
                "end": round(float(scene["end"]), 6),
                "duration": round(float(scene["duration"]), 6),
                "motion_preset": scene["motion_preset"],
                "review_status": scene["review_status"],
            }
        )
    return {
        "schema_version": "1.0",
        "episode_id": preflight["episode_id"],
        "frame_rate": preflight["frame_rate"],
        "resolution": preflight["resolution"],
        "video_codec": "H.264",
        "audio_codec": "AAC",
        "timeline_duration": round(float(preflight["timeline_duration"]), 6),
        "motion_contract": preflight["motion_contract_id"],
        "scenes": scenes,
    }


def _motion_filter(preset: str, *, duration: float, fps: int = FRAME_RATE) -> str:
    frames = max(1, round(duration * fps))
    base = (
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
    )
    centered_x = "iw/2-(iw/zoom/2)"
    centered_y = "ih/2-(ih/zoom/2)"

    if preset in {"STATIC", "HOLD"}:
        effect = f"fps={fps}"
    elif preset == "SLOW_ZOOM_IN":
        effect = (
            "zoompan="
            "z='min(1.0+on*0.08/"
            f"{frames},1.08)':x='{centered_x}':y='{centered_y}':"
            f"d=1:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:fps={fps}"
        )
    elif preset == "SLOW_ZOOM_OUT":
        effect = (
            "zoompan="
            "z='max(1.08-on*0.08/"
            f"{frames},1.0)':x='{centered_x}':y='{centered_y}':"
            f"d=1:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:fps={fps}"
        )
    elif preset in {"PAN_LEFT", "PAN_RIGHT", "PAN_UP", "PAN_DOWN"}:
        progress = f"on/{frames}"
        positions = {
            "PAN_LEFT": (f"(iw-iw/zoom)*(1-{progress})", centered_y),
            "PAN_RIGHT": (f"(iw-iw/zoom)*{progress}", centered_y),
            "PAN_UP": (centered_x, f"(ih-ih/zoom)*(1-{progress})"),
            "PAN_DOWN": (centered_x, f"(ih-ih/zoom)*{progress}"),
        }
        x, y = positions[preset]
        effect = (
            f"zoompan=z=1.08:x='{x}':y='{y}':"
            f"d=1:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:fps={fps}"
        )
    elif preset == "LIGHT_PARALLAX":
        effect = (
            "zoompan=z=1.04:"
            f"x='{centered_x}+sin(on/20)*6':y='{centered_y}':"
            f"d=1:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:fps={fps}"
        )
    elif preset == "LIGHT_SHAKE":
        effect = (
            "zoompan=z=1.02:"
            f"x='{centered_x}+sin(on/2)*2':y='{centered_y}+cos(on/2)*2':"
            f"d=1:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:fps={fps}"
        )
    elif preset == "CROSSFADE":
        fade_duration = min(0.25, duration / 4)
        fade_out = max(0.0, duration - fade_duration)
        effect = (
            f"fps={fps},fade=t=in:st=0:d={fade_duration:.3f},"
            f"fade=t=out:st={fade_out:.3f}:d={fade_duration:.3f}"
        )
    else:
        raise RenderError(f"unsupported motion preset reached render engine: {preset}")
    return f"{base},{effect},trim=duration={duration:.6f},setpts=PTS-STARTPTS"


def build_ffmpeg_command(
    preflight: Mapping[str, Any],
    *,
    ffmpeg_executable: str,
    output_path: Path,
) -> list[str]:
    """Return an argument list; it is never executed through a shell."""

    command = [ffmpeg_executable, "-hide_banner", "-y"]
    scenes = preflight["scenes"]
    for scene in scenes:
        image_path = scene.get("image_path")
        if not isinstance(image_path, Path):
            raise RenderError(f"{scene['scene_id']}: unresolved image path")
        command.extend(
            [
                "-loop",
                "1",
                "-framerate",
                str(preflight["frame_rate"]),
                "-t",
                f"{float(scene['duration']):.6f}",
                "-i",
                str(image_path),
            ]
        )
    command.extend(["-i", str(preflight["paths"]["narration"])])

    filter_parts = []
    concat_inputs = []
    for index, scene in enumerate(scenes):
        filter_parts.append(
            f"[{index}:v]{_motion_filter(scene['motion_preset'], duration=scene['duration'])}"
            f"[v{index}]"
        )
        concat_inputs.append(f"[v{index}]")
    filter_parts.append(
        "".join(concat_inputs) + f"concat=n={len(scenes)}:v=1:a=0[vout]"
    )
    audio_input = len(scenes)
    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[vout]",
            "-map",
            f"{audio_input}:a:0",
            "-c:v",
            VIDEO_CODEC,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(preflight["frame_rate"]),
            "-c:a",
            AUDIO_CODEC,
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return command


def probe_ffmpeg(
    executable: str | Path = "ffmpeg",
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str, str]:
    """Resolve FFmpeg and return ``(executable, version_line)``."""

    requested = str(executable)
    candidate = None
    requested_path = Path(requested)
    if requested_path.is_absolute() or requested_path.parent != Path("."):
        if requested_path.is_file():
            candidate = str(requested_path.resolve())
    else:
        candidate = shutil.which(requested)
    if candidate is None:
        raise RenderError(f"FFmpeg is unavailable: {requested}")

    execute = runner or subprocess.run
    try:
        result = execute(
            [candidate, "-version"],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except OSError as exc:
        raise RenderError(f"FFmpeg is unavailable: {requested} ({exc})") from exc
    if result.returncode != 0:
        raise RenderError(f"FFmpeg version check failed with exit code {result.returncode}")
    version_line = (result.stdout or result.stderr or "ffmpeg version unknown").splitlines()[0]
    return candidate, version_line


def _input_hashes(preflight: Mapping[str, Any]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, path in preflight["paths"].items():
        if isinstance(path, Path) and path.is_file():
            hashes[name] = _sha256(path)
    for scene in preflight["scenes"]:
        path = scene.get("image_path")
        if isinstance(path, Path) and path.is_file():
            hashes[f"visual:{scene['scene_id']}"] = _sha256(path)
    return dict(sorted(hashes.items()))


def _render_manifest(
    preflight: Mapping[str, Any],
    *,
    output_path: Path,
    ffmpeg_version: str,
    ffmpeg_command: Sequence[str],
    status: str,
) -> dict[str, Any]:
    hashes = _input_hashes(preflight)
    return {
        "schema_version": "1.0",
        "episode_id": preflight["episode_id"],
        "generated_at": _utc_now(),
        "narration": {
            "file": preflight["sources"]["narration"],
            "sha256": hashes.get("narration"),
        },
        "timing": {
            "file": preflight["sources"]["timing"],
            "sha256": hashes.get("timing"),
        },
        "scene_map": {
            "file": preflight["sources"]["scene_map"],
            "sha256": hashes.get("scene_map"),
        },
        "visual_manifest": {
            "file": preflight["sources"]["visual_manifest"],
            "sha256": hashes.get("visual_manifest"),
        },
        "input_hashes": hashes,
        "motion_contract": {
            "id": preflight["motion_contract_id"],
            "file": preflight["sources"]["motion_contract"],
            "sha256": hashes.get("motion_contract"),
        },
        "output_file": output_path.resolve().as_posix(),
        "ffmpeg": {
            "command": list(ffmpeg_command),
            "version": ffmpeg_version,
        },
        "status": status,
    }


def execute_final_render(
    *,
    root: str | Path = ROOT,
    episode_context: EpisodeContext | None = None,
    narration_path: str | Path | None = None,
    timing_path: str | Path | None = None,
    scene_map_path: str | Path | None = None,
    visual_scenes_path: str | Path | None = None,
    visual_manifest_path: str | Path | None = None,
    motion_contract_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    ffmpeg: str | Path = "ffmpeg",
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Run the complete guarded render flow or its non-rendering dry run."""

    project_root = Path(root).resolve()
    context = episode_context or resolve_active_episode(project_root)
    if _episode_stage(context) == "visual_qualification":
        raise FinalRenderBlocked(FINAL_RENDER_BLOCKED)

    preflight = run_preflight(
        root=project_root,
        episode_context=context,
        narration_path=narration_path,
        timing_path=timing_path,
        scene_map_path=scene_map_path,
        visual_scenes_path=visual_scenes_path,
        visual_manifest_path=visual_manifest_path,
        motion_contract_path=motion_contract_path,
        require_production=True,
    )
    if not preflight["passed"]:
        raise RenderError("PREFLIGHT_FAILED: " + "; ".join(preflight["errors"]))

    ffmpeg_executable, ffmpeg_version = probe_ffmpeg(ffmpeg, runner=runner)
    destination = (
        Path(output_dir).resolve()
        if output_dir is not None
        else project_root / "output" / "render" / context.episode_id
    )
    output_path = destination / "final.mp4"
    temporary_output = destination / ".final.tmp.mp4"
    plan = build_render_plan(preflight)
    command = build_ffmpeg_command(
        preflight,
        ffmpeg_executable=ffmpeg_executable,
        output_path=temporary_output,
    )

    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "render_plan.json", plan)
    if dry_run:
        manifest = _render_manifest(
            preflight,
            output_path=output_path,
            ffmpeg_version=ffmpeg_version,
            ffmpeg_command=command,
            status="DRY_RUN",
        )
        _write_json(destination / "render_manifest.json", manifest)
        return {"preflight": preflight, "plan": plan, "command": command, "manifest": manifest}

    execute = runner or subprocess.run
    try:
        result = execute(command, check=False, capture_output=True, text=True, shell=False)
    except OSError as exc:
        raise RenderError(f"FFmpeg execution failed: {exc}") from exc
    if result.returncode != 0:
        temporary_output.unlink(missing_ok=True)
        manifest = _render_manifest(
            preflight,
            output_path=output_path,
            ffmpeg_version=ffmpeg_version,
            ffmpeg_command=command,
            status="FAILED",
        )
        manifest["error"] = (result.stderr or "FFmpeg failed").strip()[-2000:]
        _write_json(destination / "render_manifest.json", manifest)
        raise RenderError(f"FFmpeg failed with exit code {result.returncode}")
    if not temporary_output.is_file():
        raise RenderError("FFmpeg reported success but did not create the expected output")
    temporary_output.replace(output_path)

    manifest = _render_manifest(
        preflight,
        output_path=output_path,
        ffmpeg_version=ffmpeg_version,
        ffmpeg_command=command,
        status="COMPLETED",
    )
    _write_json(destination / "render_manifest.json", manifest)
    return {"preflight": preflight, "plan": plan, "command": command, "manifest": manifest}


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audio", type=Path, help="Official narration WAV.")
    parser.add_argument("--timing", type=Path, help="Official narration timing JSON.")
    parser.add_argument("--scene-map", type=Path)
    parser.add_argument("--visual-scenes", type=Path)
    parser.add_argument("--visual-manifest", type=Path)
    parser.add_argument("--motion-contract", type=Path)


def build_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed final render pipeline.", add_help=add_help
    )
    commands = parser.add_subparsers(dest="render_command", required=True)
    validate = commands.add_parser("validate", help="Run preflight only; never runs FFmpeg.")
    _add_source_arguments(validate)

    final = commands.add_parser("final", help="Render the approved episode timeline.")
    _add_source_arguments(final)
    final.add_argument("--output-dir", type=Path)
    final.add_argument("--ffmpeg", default="ffmpeg")
    final.add_argument("--dry-run", action="store_true")
    return parser


def _preflight_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return run_preflight(
        narration_path=args.audio,
        timing_path=args.timing,
        scene_map_path=args.scene_map,
        visual_scenes_path=args.visual_scenes,
        visual_manifest_path=args.visual_manifest,
        motion_contract_path=args.motion_contract,
        require_production=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.render_command == "validate":
        try:
            result = _preflight_from_args(args)
        except (RenderError, OSError, ValueError, KeyError) as exc:
            print(f"PREFLIGHT: FAIL\n- {exc}")
            return 1
        print(f"PREFLIGHT: {'PASS' if result['passed'] else 'FAIL'}")
        for warning in result["warnings"]:
            print(f"WARNING: {warning}")
        for error in result["errors"]:
            print(f"FAIL: {error}")
        return 0 if result["passed"] else 1

    try:
        result = execute_final_render(
            narration_path=args.audio,
            timing_path=args.timing,
            scene_map_path=args.scene_map,
            visual_scenes_path=args.visual_scenes,
            visual_manifest_path=args.visual_manifest,
            motion_contract_path=args.motion_contract,
            output_dir=args.output_dir,
            ffmpeg=args.ffmpeg,
            dry_run=args.dry_run,
        )
    except FinalRenderBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (RenderError, OSError, ValueError, KeyError) as exc:
        print(f"RENDER_FAILED: {exc}", file=sys.stderr)
        return 1

    print("PREFLIGHT: PASS")
    print(f"RENDER_PLAN: output/render/{result['manifest']['episode_id']}/render_plan.json")
    print("FFMPEG_COMMAND: " + json.dumps(result["command"], ensure_ascii=False))
    print(f"STATUS: {result['manifest']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
