import unittest
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.live_rs485_oil_monitor import DatBlockParser, RollingOilDecider


class DatBlockParserTests(unittest.TestCase):
    def test_emits_block_after_expected_sample_count(self):
        parser = DatBlockParser(expected_samples=3)
        lines = [
            "==========>>>",
            "",
            "Pos:4",
            "Dis:150.04mm",
            "data - cfar",
            "30088,26253",
            "62714,25886",
            "90632,25142",
        ]

        block = None
        for line in lines:
            candidate = parser.feed_line(line)
            if candidate is not None:
                block = candidate

        self.assertIsNotNone(block)
        self.assertEqual(block.pos, 4)
        self.assertAlmostEqual(block.dis_mm, 150.04)
        np.testing.assert_allclose(block.data, [30088, 62714, 90632])
        np.testing.assert_allclose(block.cfar, [26253, 25886, 25142])

    def test_emits_near_complete_block_on_next_header_and_resamples(self):
        parser = DatBlockParser(expected_samples=5, min_samples=4)
        lines = [
            "Pos:4",
            "Dis:150.04mm",
            "data - cfar",
            "10,100",
            "20,200",
            "30,300",
            "40,400",
            "Pos:5",
        ]

        block = None
        for line in lines:
            candidate = parser.feed_line(line)
            if candidate is not None:
                block = candidate

        self.assertIsNotNone(block)
        self.assertEqual(block.pos, 4)
        self.assertAlmostEqual(block.dis_mm, 150.04)
        np.testing.assert_allclose(block.data, [10.0, 17.5, 25.0, 32.5, 40.0])
        np.testing.assert_allclose(block.cfar, [100.0, 175.0, 250.0, 325.0, 400.0])


class RollingOilDeciderTests(unittest.TestCase):
    def test_marks_uncertain_when_file_like_window_is_mixed(self):
        decider = RollingOilDecider(window_size=7, min_blocks=7, block_threshold=0.42)
        result = None
        for prob in [0.7626, 0.0550, 0.3586, 0.4599, 0.7164, 0.7203, 0.8768]:
            result = decider.update(prob)

        self.assertEqual(result["status"], "uncertain")
        self.assertAlmostEqual(result["mean_prob"], 0.5642, places=3)
        self.assertAlmostEqual(result["positive_fraction"], 5 / 7, places=3)

    def test_marks_oil_only_when_probability_and_consistency_are_high(self):
        decider = RollingOilDecider(window_size=5, min_blocks=5, block_threshold=0.42)
        result = None
        for prob in [0.96, 0.91, 0.99, 0.94, 0.98]:
            result = decider.update(prob)

        self.assertEqual(result["status"], "oil")

    def test_marks_no_oil_only_when_probability_and_consistency_are_low(self):
        decider = RollingOilDecider(window_size=5, min_blocks=5, block_threshold=0.42)
        result = None
        for prob in [0.03, 0.10, 0.18, 0.07, 0.21]:
            result = decider.update(prob)

        self.assertEqual(result["status"], "no_oil")


if __name__ == "__main__":
    unittest.main()
