import unittest
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.live_rs485_oil_monitor import DatBlock
from scripts.live_rs485_oil_gui import (
    LiveSettings,
    PredictionSnapshot,
    build_request_bytes,
    calibration_dat_path,
    display_status_for_snapshot,
    format_block_as_dat,
    has_probe_position_changed,
)


class LiveRs485OilGuiTests(unittest.TestCase):
    def test_build_request_bytes_uses_ascii_command(self):
        self.assertEqual(build_request_bytes("!TEST:2"), b"!TEST:2")

    def test_position_change_resets_when_pos_or_distance_changes(self):
        self.assertFalse(has_probe_position_changed(None, None, 3, 112.53, 5.0))
        self.assertFalse(has_probe_position_changed(3, 112.53, 3, 115.00, 5.0))
        self.assertTrue(has_probe_position_changed(3, 112.53, 3, 118.00, 5.0))
        self.assertTrue(has_probe_position_changed(3, 112.53, 4, 112.53, 5.0))

    def test_default_settings_use_one_second_fast_single_block_judgment(self):
        settings = LiveSettings()
        self.assertEqual(settings.request_interval_seconds, 1.0)
        self.assertEqual(settings.min_blocks, 1)
        self.assertTrue(settings.fast_single_block_display)

    def test_fast_display_uses_current_block_prediction(self):
        snapshot = PredictionSnapshot(
            timestamp="2026-06-22 12:00:00.000",
            block_index=0,
            pos=3,
            dis_mm=112.53,
            prob_oil=0.63,
            block_pred="有油",
            rolling_status="采集中",
            rolling_mean_prob=0.63,
            rolling_median_prob=0.63,
            positive_fraction=1.0,
            rolling_block_count=1,
        )

        self.assertEqual(display_status_for_snapshot(snapshot, LiveSettings()), "有油")

    def test_format_block_as_dat_preserves_waveform_pairs(self):
        block = DatBlock(
            pos=3,
            dis_mm=112.53,
            data=np.asarray([10.0, 20.0]),
            cfar=np.asarray([1.0, 2.0]),
        )

        text = format_block_as_dat(block)

        self.assertIn("Pos:3", text)
        self.assertIn("Dis:112.53mm", text)
        self.assertIn("data - cfar", text)
        self.assertIn("10,1", text)
        self.assertIn("20,2", text)

    def test_calibration_dat_path_uses_label_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = calibration_dat_path(Path(tmp), "无油", "20260622_120000")

        self.assertEqual(path.name, "rs485_20260622_120000.DAT")
        self.assertEqual(path.parent.name, "无油")


if __name__ == "__main__":
    unittest.main()
