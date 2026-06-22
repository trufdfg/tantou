import tempfile
import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.train_calibrated_oil_classifier import calibration_group_splits, load_calibration_records


def write_dat(path: Path, *, pos: int = 3, dis_mm: float = 112.53) -> None:
    rows = "\n".join(f"{1000 + i},{900 + i}" for i in range(20))
    path.write_text(
        f"==========>>>\n\nPos:{pos}\nDis:{dis_mm:.2f}mm\ndata - cfar\n{rows}\n",
        encoding="utf-8",
    )


class TrainCalibratedOilClassifierTests(unittest.TestCase):
    def test_load_calibration_records_uses_label_dirs_and_unique_file_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            no_oil_dir = root / "无油"
            oil_dir = root / "有油"
            no_oil_dir.mkdir()
            oil_dir.mkdir()
            write_dat(no_oil_dir / "same_name.DAT")
            write_dat(oil_dir / "same_name.DAT")

            records = load_calibration_records(root)

        self.assertEqual(len(records), 2)
        self.assertEqual(sorted(record.label_name for record in records), ["无油", "有油"])
        self.assertEqual(sorted(record.label for record in records), [0, 1])
        self.assertEqual(len({record.file_id for record in records}), 2)

    def test_calibration_group_splits_keep_both_classes_in_training(self):
        labels = [0, 0, 1, 1]
        groups = ["no_a", "no_b", "oil_a", "oil_b"]

        splits = list(calibration_group_splits(labels, groups))

        self.assertEqual(len(splits), 4)
        for train_idx, valid_idx in splits:
            train_labels = {labels[index] for index in train_idx}
            self.assertEqual(train_labels, {0, 1})
            self.assertEqual(len(valid_idx), 1)


if __name__ == "__main__":
    unittest.main()
