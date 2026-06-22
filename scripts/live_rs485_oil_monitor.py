from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

import joblib
import numpy as np
import pandas as pd
import serial


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.train_oil_classifier import Record, extract_features  # noqa: E402


@dataclass
class DatBlock:
    pos: int
    dis_mm: float
    data: np.ndarray
    cfar: np.ndarray


class DatBlockParser:
    def __init__(self, expected_samples: int = 220, min_samples: int | None = None) -> None:
        self.expected_samples = expected_samples
        self.min_samples = expected_samples if min_samples is None else min_samples
        if self.min_samples < 1 or self.min_samples > self.expected_samples:
            raise ValueError("min_samples must be between 1 and expected_samples")
        self.pos: int | None = None
        self.dis_mm: float | None = None
        self.collecting = False
        self.data: list[float] = []
        self.cfar: list[float] = []

    def feed_line(self, line: str) -> DatBlock | None:
        clean = line.strip()
        if not clean:
            return None
        if clean.lower().startswith("pos:"):
            previous_block = self._build_block_if_ready(self.min_samples)
            self._reset_all()
            self.pos = int(clean.split(":", 1)[1].strip())
            return previous_block
        if clean.lower().startswith("dis:"):
            value = clean.split(":", 1)[1].strip().lower().replace("mm", "")
            self.dis_mm = float(value)
            return None
        if clean.lower().replace(" ", "") == "data-cfar":
            self._reset_rows()
            self.collecting = True
            return None
        if not self.collecting or "," not in clean:
            return None

        left, right = clean.split(",", 1)
        try:
            data_value = float(left.strip())
            cfar_value = float(right.strip())
        except ValueError:
            return None

        self.data.append(data_value)
        self.cfar.append(cfar_value)
        if len(self.data) >= self.expected_samples:
            block = self._build_block_if_ready(self.expected_samples)
            self._reset_all()
            return block
        return None

    def _build_block_if_ready(self, min_required: int) -> DatBlock | None:
        if self.pos is None or self.dis_mm is None:
            return None
        if len(self.data) < min_required or len(self.cfar) < min_required:
            return None
        return DatBlock(
            pos=self.pos,
            dis_mm=self.dis_mm,
            data=resample_to_length(self.data, self.expected_samples),
            cfar=resample_to_length(self.cfar, self.expected_samples),
        )

    def _reset_rows(self) -> None:
        self.collecting = False
        self.data = []
        self.cfar = []

    def _reset_all(self) -> None:
        self.pos = None
        self.dis_mm = None
        self._reset_rows()


def resample_to_length(values: list[float], target_length: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if len(arr) == target_length:
        return arr[:target_length]
    if len(arr) == 1:
        return np.repeat(arr[0], target_length)
    old_x = np.linspace(0.0, 1.0, len(arr))
    new_x = np.linspace(0.0, 1.0, target_length)
    return np.interp(new_x, old_x, arr)


class RollingOilDecider:
    def __init__(
        self,
        *,
        window_size: int = 7,
        min_blocks: int = 5,
        block_threshold: float = 0.42,
        oil_threshold: float = 0.85,
        no_oil_threshold: float = 0.25,
        consistency: float = 0.80,
    ) -> None:
        self.window_size = window_size
        self.min_blocks = min_blocks
        self.block_threshold = block_threshold
        self.oil_threshold = oil_threshold
        self.no_oil_threshold = no_oil_threshold
        self.consistency = consistency
        self.probabilities: deque[float] = deque(maxlen=window_size)

    def update(self, prob_oil: float) -> dict[str, float | int | str]:
        self.probabilities.append(float(prob_oil))
        values = np.asarray(self.probabilities, dtype=float)
        positive_fraction = float(np.mean(values >= self.block_threshold))
        mean_prob = float(np.mean(values))
        median_prob = float(np.median(values))

        if len(values) < self.min_blocks:
            status = "collecting"
        elif mean_prob >= self.oil_threshold and positive_fraction >= self.consistency:
            status = "oil"
        elif mean_prob <= self.no_oil_threshold and positive_fraction <= (1.0 - self.consistency):
            status = "no_oil"
        else:
            status = "uncertain"

        return {
            "status": status,
            "mean_prob": mean_prob,
            "median_prob": median_prob,
            "positive_fraction": positive_fraction,
            "block_count": int(len(values)),
        }


def load_model_bundle(model_path: Path) -> dict:
    bundle = joblib.load(model_path)
    required = {"model", "feature_columns", "threshold"}
    missing = sorted(required - set(bundle))
    if missing:
        raise ValueError(f"Model bundle missing keys: {missing}")
    return bundle


def predict_block(block: DatBlock, bundle: dict, block_index: int) -> float:
    record = Record(
        split="live",
        file_path=Path("live_rs485.DAT"),
        file_id="live_rs485",
        block_index=block_index,
        pos=block.pos,
        dis_mm=block.dis_mm,
        data=block.data,
        cfar=block.cfar,
    )
    row = extract_features(record)
    frame = pd.DataFrame([row])
    feature_columns = list(bundle["feature_columns"])
    for column in feature_columns:
        if column not in frame.columns:
            frame[column] = np.nan
    model = bundle["model"]
    proba = model.predict_proba(frame[feature_columns])
    positive_index = list(model.classes_).index(1)
    return float(proba[:, positive_index][0])


def status_cn(status: str) -> str:
    return {
        "collecting": "采集中",
        "oil": "有油",
        "no_oil": "无油",
        "uncertain": "不确定-建议复测",
    }.get(status, status)


def open_logs(output_dir: Path) -> tuple[TextIO, csv.DictWriter, TextIO]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prediction_file = (output_dir / f"live_predictions_{stamp}.csv").open("w", encoding="utf-8-sig", newline="")
    raw_file = (output_dir / f"live_raw_serial_{stamp}.txt").open("w", encoding="utf-8")
    writer = csv.DictWriter(
        prediction_file,
        fieldnames=[
            "timestamp",
            "block_index",
            "pos",
            "dis_mm",
            "prob_oil",
            "block_pred",
            "rolling_status",
            "rolling_mean_prob",
            "rolling_median_prob",
            "positive_fraction",
            "rolling_block_count",
        ],
    )
    writer.writeheader()
    return prediction_file, writer, raw_file


def run_monitor(args: argparse.Namespace) -> None:
    bundle = load_model_bundle(args.model_path)
    parser = DatBlockParser(expected_samples=args.expected_samples, min_samples=args.min_samples)
    decider = RollingOilDecider(
        window_size=args.window_size,
        min_blocks=args.min_blocks,
        block_threshold=float(bundle["threshold"]),
        oil_threshold=args.oil_threshold,
        no_oil_threshold=args.no_oil_threshold,
        consistency=args.consistency,
    )
    prediction_file, writer, raw_file = open_logs(args.output_dir)
    start = time.monotonic()
    block_index = 0
    line_count = 0
    byte_count = 0
    request_bytes = bytes.fromhex(args.request_hex.replace(" ", "")) if args.request_hex else b""
    next_request_at = start

    print(
        f"打开串口 {args.port}, baudrate={args.baudrate}, expected_samples={args.expected_samples}. "
        f"min_samples={args.min_samples}. "
        f"模型阈值={float(bundle['threshold']):.2f}, 滚动窗口={args.window_size}块."
    )
    print("输出列: 时间 块号 Pos Dis(mm) 单块有油概率 单块判断 滚动判断 滚动均值 阳性块比例")
    print("-" * 110)

    try:
        with serial.Serial(args.port, args.baudrate, timeout=args.timeout) as ser:
            while True:
                if args.duration and time.monotonic() - start >= args.duration:
                    print(f"达到采集时长 {args.duration:.1f}s，停止。")
                    break
                if args.max_blocks and block_index >= args.max_blocks:
                    print(f"达到最大记录块数 {args.max_blocks}，停止。")
                    break

                now = time.monotonic()
                if request_bytes and now >= next_request_at:
                    ser.write(request_bytes)
                    ser.flush()
                    next_request_at = now + args.request_interval

                raw = ser.readline()
                if not raw:
                    continue
                byte_count += len(raw)
                line_count += 1
                line = raw.decode(args.encoding, errors="ignore").strip()
                if line:
                    raw_file.write(line + "\n")
                    raw_file.flush()
                block = parser.feed_line(line)
                if block is None:
                    continue

                prob_oil = predict_block(block, bundle, block_index)
                block_pred = "有油" if prob_oil >= float(bundle["threshold"]) else "无油"
                rolling = decider.update(prob_oil)
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                row = {
                    "timestamp": timestamp,
                    "block_index": block_index,
                    "pos": block.pos,
                    "dis_mm": f"{block.dis_mm:.2f}",
                    "prob_oil": f"{prob_oil:.6f}",
                    "block_pred": block_pred,
                    "rolling_status": status_cn(str(rolling["status"])),
                    "rolling_mean_prob": f"{float(rolling['mean_prob']):.6f}",
                    "rolling_median_prob": f"{float(rolling['median_prob']):.6f}",
                    "positive_fraction": f"{float(rolling['positive_fraction']):.6f}",
                    "rolling_block_count": int(rolling["block_count"]),
                }
                writer.writerow(row)
                prediction_file.flush()
                print(
                    f"{timestamp}  #{block_index:04d}  Pos={block.pos:<2d} Dis={block.dis_mm:7.2f}  "
                    f"P(有油)={prob_oil:6.1%}  单块={block_pred:<2s}  "
                    f"滚动={row['rolling_status']:<9s}  均值={float(rolling['mean_prob']):6.1%}  "
                    f"阳性块={float(rolling['positive_fraction']):5.1%}"
                )
                block_index += 1
    except serial.SerialException as exc:
        raise SystemExit(f"串口打开或读取失败: {exc}") from exc
    except KeyboardInterrupt:
        print("收到 Ctrl+C，停止采集。")
    finally:
        prediction_file.close()
        raw_file.close()
        print(f"串口行数: {line_count}, 有效记录块: {block_index}")
        print(f"串口字节数: {byte_count}")
        print(f"预测日志: {prediction_file.name}")
        print(f"原始串口日志: {raw_file.name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read DAT-like RS485 blocks and classify oil/no-oil live.")
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0.0, help="Seconds to run. 0 means until stopped.")
    parser.add_argument("--max-blocks", type=int, default=0)
    parser.add_argument("--expected-samples", type=int, default=220)
    parser.add_argument("--min-samples", type=int, default=210)
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--request-hex", default="", help="Optional hex command sent periodically, e.g. '01 03 00 00 00 10'.")
    parser.add_argument("--request-interval", type=float, default=1.0)
    parser.add_argument("--window-size", type=int, default=7)
    parser.add_argument("--min-blocks", type=int, default=5)
    parser.add_argument("--oil-threshold", type=float, default=0.85)
    parser.add_argument("--no-oil-threshold", type=float, default=0.25)
    parser.add_argument("--consistency", type=float, default=0.80)
    parser.add_argument("--model-path", type=Path, default=ROOT / "outputs" / "oil_classifier_model.joblib")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "live_rs485")
    return parser.parse_args()


if __name__ == "__main__":
    run_monitor(parse_args())
