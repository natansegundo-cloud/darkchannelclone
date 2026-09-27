from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.narration import config
from src.narration.cleanup import (
    DEFAULT_BEATS,
    find_humberto_source,
    run_humberto_cleanup,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "episodios" / "CO-001" / "roteiro_narracao.md"
DELIVERY = ROOT / "episodios" / "CO-001" / "narration_delivery.json"
BENCHMARK_ROOT = ROOT / "output" / "audio" / "CO-001" / "voice_benchmark"


class NarrationCleanupTest(unittest.TestCase):
    def test_local_cleanup_reuses_one_raw_without_azure_or_timing_changes(self):
        source = find_humberto_source(BENCHMARK_ROOT)
        official_paths = [
            config.DEFAULT_OUTPUT / "narration.wav",
            config.DEFAULT_OUTPUT / "timing.json",
            config.DEFAULT_OUTPUT / "raw" / "B001.wav",
            config.DEFAULT_OUTPUT / "raw" / "B001.json",
        ]
        official_before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in official_paths
        }
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            root = Path(name)
            fake_ffmpeg = root / "ffmpeg.exe"
            fake_ffmpeg.write_bytes(b"fixture")

            def runner(command, **_kwargs):
                calls.append(list(command))
                source_path = Path(command[command.index("-i") + 1])
                target_path = Path(command[-1])
                shutil.copyfile(source_path, target_path)
                return subprocess.CompletedProcess(command, 0, "", "")

            manifest = run_humberto_cleanup(
                source_raw_dir=source,
                benchmark_root=root / "voice_benchmark",
                script_path=SCRIPT,
                delivery_path=DELIVERY,
                episode_id="CO-001",
                run_id="cleanup-test",
                ffmpeg=fake_ffmpeg,
                runner=runner,
            )
            self.assertEqual(manifest["azure_calls_total"], 0)
            self.assertEqual(len(calls), 15)
            self.assertTrue(any("afftdn" in command[command.index("-af") + 1] for command in calls))
            self.assertTrue(any("lowpass=f=10500" in command[command.index("-af") + 1] for command in calls))
            variants = manifest["variants"]
            self.assertEqual(set(variants), {
                "baseline", "gentle_denoise", "gentle_lowpass", "gentle_denoise_lowpass"
            })
            for beat_id in DEFAULT_BEATS:
                rows = [variant["beats"][beat_id] for variant in variants.values()]
                self.assertEqual(len({row["raw_audio_sha256"] for row in rows}), 1)
                self.assertEqual(len({row["synthesis_id"] for row in rows}), 1)
                self.assertEqual(len({row["processed"]["duration"] for row in rows}), 1)
                self.assertTrue(all(row["same_synthesis_audio_timing"] for row in rows))
                self.assertTrue(all(row["timing_quality"] == "WORD_BOUNDARY_REAL" for row in rows))
                self.assertTrue(all(row["processed"]["clipping_samples"] == 0 for row in rows))
                self.assertTrue(all(abs(row["processed"]["sample_peak_dbfs"] + 10) < .02 for row in rows))
            comparison_durations = {
                variant["comparison_duration"] for variant in variants.values()
            }
            self.assertEqual(len(comparison_durations), 1)

        official_after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in official_paths
        }
        self.assertEqual(official_before, official_after)


if __name__ == "__main__":
    unittest.main()
