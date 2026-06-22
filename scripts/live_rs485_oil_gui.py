from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import serial


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.live_rs485_oil_monitor import (
    DatBlock,
    DatBlockParser,
    RollingOilDecider,
    load_model_bundle,
    open_logs,
    predict_block,
    status_cn,
)


# PyCharm 运行时优先改这里。当前 COM5 参数已经用实测数据验证过。
PORT = "COM5"
BAUDRATE = 115200
REQUEST_TEXT = "!TEST:2"
REQUEST_INTERVAL_SECONDS = 1.0
SERIAL_TIMEOUT_SECONDS = 0.2

EXPECTED_SAMPLES = 220
MIN_SAMPLES = 210
POSITION_RESET_MM = 5.0

WINDOW_SIZE = 7
MIN_BLOCKS = 1
OIL_THRESHOLD = 0.85
NO_OIL_THRESHOLD = 0.25
CONSISTENCY = 0.80
FAST_SINGLE_BLOCK_DISPLAY = True

MODEL_PATH = ROOT / "outputs" / "oil_classifier_model.joblib"
OUTPUT_DIR = ROOT / "outputs" / "live_rs485_gui"
CALIBRATION_DIR = ROOT / "shuju" / "实机标定数据"
CALIBRATION_LABELS = ("无油", "有油")


@dataclass(frozen=True)
class LiveSettings:
    port: str = PORT
    baudrate: int = BAUDRATE
    request_text: str = REQUEST_TEXT
    request_interval_seconds: float = REQUEST_INTERVAL_SECONDS
    serial_timeout_seconds: float = SERIAL_TIMEOUT_SECONDS
    expected_samples: int = EXPECTED_SAMPLES
    min_samples: int = MIN_SAMPLES
    position_reset_mm: float = POSITION_RESET_MM
    window_size: int = WINDOW_SIZE
    min_blocks: int = MIN_BLOCKS
    oil_threshold: float = OIL_THRESHOLD
    no_oil_threshold: float = NO_OIL_THRESHOLD
    consistency: float = CONSISTENCY
    fast_single_block_display: bool = FAST_SINGLE_BLOCK_DISPLAY
    model_path: Path = MODEL_PATH
    output_dir: Path = OUTPUT_DIR
    calibration_dir: Path = CALIBRATION_DIR


@dataclass(frozen=True)
class PredictionSnapshot:
    timestamp: str
    block_index: int
    pos: int
    dis_mm: float
    prob_oil: float
    block_pred: str
    rolling_status: str
    rolling_mean_prob: float
    rolling_median_prob: float
    positive_fraction: float
    rolling_block_count: int


def build_request_bytes(command_text: str) -> bytes:
    return command_text.encode("ascii")


def _format_dat_number(value: float) -> str:
    rounded = round(float(value))
    if abs(float(value) - rounded) < 1e-6:
        return str(int(rounded))
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def format_block_as_dat(block: DatBlock) -> str:
    lines = [
        "==========>>>",
        "",
        f"Pos:{block.pos}",
        f"Dis:{block.dis_mm:.2f}mm",
        "data - cfar",
    ]
    for data_value, cfar_value in zip(block.data, block.cfar):
        lines.append(f"{_format_dat_number(data_value)},{_format_dat_number(cfar_value)}")
    return "\n".join(lines) + "\n\n"


def calibration_dat_path(calibration_dir: Path, label_name: str, session_id: str) -> Path:
    return calibration_dir / label_name / f"rs485_{session_id}.DAT"


def append_calibration_block(calibration_dir: Path, label_name: str, session_id: str, block: DatBlock) -> Path:
    if label_name not in CALIBRATION_LABELS:
        raise ValueError(f"Unsupported calibration label: {label_name}")
    path = calibration_dat_path(calibration_dir, label_name, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(format_block_as_dat(block))
    return path


def has_probe_position_changed(
    last_pos: int | None,
    last_dis_mm: float | None,
    new_pos: int,
    new_dis_mm: float,
    tolerance_mm: float,
) -> bool:
    if last_pos is None or last_dis_mm is None:
        return False
    if new_pos != last_pos:
        return True
    return abs(new_dis_mm - last_dis_mm) > tolerance_mm


def make_decider(bundle: dict, settings: LiveSettings) -> RollingOilDecider:
    return RollingOilDecider(
        window_size=settings.window_size,
        min_blocks=settings.min_blocks,
        block_threshold=float(bundle["threshold"]),
        oil_threshold=settings.oil_threshold,
        no_oil_threshold=settings.no_oil_threshold,
        consistency=settings.consistency,
    )


def display_status_for_snapshot(snapshot: PredictionSnapshot, settings: LiveSettings) -> str:
    if settings.fast_single_block_display:
        return snapshot.block_pred
    return snapshot.rolling_status


class CalibrationLabelState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._label_name: str | None = None

    def set_label(self, label_name: str | None) -> None:
        if label_name is not None and label_name not in CALIBRATION_LABELS:
            raise ValueError(f"Unsupported calibration label: {label_name}")
        with self._lock:
            self._label_name = label_name

    def get_label(self) -> str | None:
        with self._lock:
            return self._label_name


class SerialPredictionWorker(threading.Thread):
    def __init__(
        self,
        settings: LiveSettings,
        output_queue: "queue.Queue[dict[str, object]]",
        stop_event: threading.Event,
        label_state: CalibrationLabelState,
    ) -> None:
        super().__init__(daemon=True)
        self.settings = settings
        self.output_queue = output_queue
        self.stop_event = stop_event
        self.label_state = label_state

    def run(self) -> None:
        prediction_file = None
        raw_file = None
        block_index = 0
        line_count = 0
        byte_count = 0
        last_pos: int | None = None
        last_dis_mm: float | None = None
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        calibration_counts = {"无油": 0, "有油": 0}

        try:
            bundle = load_model_bundle(self.settings.model_path)
            parser = DatBlockParser(
                expected_samples=self.settings.expected_samples,
                min_samples=self.settings.min_samples,
            )
            decider = make_decider(bundle, self.settings)
            request_bytes = build_request_bytes(self.settings.request_text)
            prediction_file, writer, raw_file = open_logs(self.settings.output_dir)
            self.output_queue.put(
                {
                    "type": "info",
                    "message": (
                        f"串口已打开准备采集: {self.settings.port}, "
                        f"{self.settings.baudrate}, 命令 {self.settings.request_text}, "
                        f"{self.settings.request_interval_seconds:.1f}秒/次"
                    ),
                }
            )

            with serial.Serial(
                self.settings.port,
                self.settings.baudrate,
                timeout=self.settings.serial_timeout_seconds,
            ) as ser:
                next_request_at = time.monotonic()
                while not self.stop_event.is_set():
                    now = time.monotonic()
                    if now >= next_request_at:
                        ser.write(request_bytes)
                        ser.flush()
                        next_request_at = now + self.settings.request_interval_seconds

                    raw = ser.readline()
                    if not raw:
                        continue
                    line_count += 1
                    byte_count += len(raw)
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if line:
                        raw_file.write(line + "\n")
                        raw_file.flush()

                    block = parser.feed_line(line)
                    if block is None:
                        continue

                    calibration_label = self.label_state.get_label()
                    if calibration_label:
                        calibration_path = append_calibration_block(
                            self.settings.calibration_dir,
                            calibration_label,
                            session_id,
                            block,
                        )
                        calibration_counts[calibration_label] += 1
                        self.output_queue.put(
                            {
                                "type": "calibration",
                                "message": (
                                    f"正在记录{calibration_label}标定: "
                                    f"{calibration_counts[calibration_label]}块 -> {calibration_path}"
                                ),
                            }
                        )

                    if has_probe_position_changed(
                        last_pos,
                        last_dis_mm,
                        block.pos,
                        block.dis_mm,
                        self.settings.position_reset_mm,
                    ):
                        decider = make_decider(bundle, self.settings)
                        self.output_queue.put(
                            {
                                "type": "info",
                                "message": (
                                    f"探头位置变化: Pos {last_pos}->{block.pos}, "
                                    f"Dis {last_dis_mm:.2f}->{block.dis_mm:.2f} mm，"
                                    "滚动窗口已重置"
                                ),
                            }
                        )
                    last_pos = block.pos
                    last_dis_mm = block.dis_mm

                    prob_oil = predict_block(block, bundle, block_index)
                    block_pred = "有油" if prob_oil >= float(bundle["threshold"]) else "无油"
                    rolling = decider.update(prob_oil)
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    snapshot = PredictionSnapshot(
                        timestamp=timestamp,
                        block_index=block_index,
                        pos=block.pos,
                        dis_mm=block.dis_mm,
                        prob_oil=prob_oil,
                        block_pred=block_pred,
                        rolling_status=status_cn(str(rolling["status"])),
                        rolling_mean_prob=float(rolling["mean_prob"]),
                        rolling_median_prob=float(rolling["median_prob"]),
                        positive_fraction=float(rolling["positive_fraction"]),
                        rolling_block_count=int(rolling["block_count"]),
                    )
                    writer.writerow(
                        {
                            "timestamp": snapshot.timestamp,
                            "block_index": snapshot.block_index,
                            "pos": snapshot.pos,
                            "dis_mm": f"{snapshot.dis_mm:.2f}",
                            "prob_oil": f"{snapshot.prob_oil:.6f}",
                            "block_pred": snapshot.block_pred,
                            "rolling_status": snapshot.rolling_status,
                            "rolling_mean_prob": f"{snapshot.rolling_mean_prob:.6f}",
                            "rolling_median_prob": f"{snapshot.rolling_median_prob:.6f}",
                            "positive_fraction": f"{snapshot.positive_fraction:.6f}",
                            "rolling_block_count": snapshot.rolling_block_count,
                        }
                    )
                    prediction_file.flush()
                    self.output_queue.put({"type": "prediction", "snapshot": snapshot})
                    block_index += 1
        except Exception as exc:
            self.output_queue.put({"type": "error", "message": str(exc)})
        finally:
            prediction_path = prediction_file.name if prediction_file is not None else ""
            raw_path = raw_file.name if raw_file is not None else ""
            if prediction_file is not None:
                prediction_file.close()
            if raw_file is not None:
                raw_file.close()
            self.output_queue.put(
                {
                    "type": "stopped",
                    "message": (
                        f"采集停止。有效块 {block_index}，串口行 {line_count}，"
                        f"字节 {byte_count}。预测日志: {prediction_path} 原始日志: {raw_path}"
                    ),
                }
            )


class OilPopupApp:
    def __init__(self, root: tk.Tk, settings: LiveSettings) -> None:
        self.root = root
        self.settings = settings
        self.output_queue: "queue.Queue[dict[str, object]]" = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.worker: SerialPredictionWorker | None = None
        self.label_state = CalibrationLabelState()

        self.root.title("太赫兹探头油液实时判断")
        self.root.geometry("560x470")
        self.root.minsize(520, 430)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#f3f4f6")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._build_ui()
        self.start_worker()
        self.root.after(100, self.poll_queue)

    def _build_ui(self) -> None:
        main = tk.Frame(self.root, bg="#f3f4f6", padx=18, pady=18)
        main.pack(fill=tk.BOTH, expand=True)

        title = tk.Label(
            main,
            text="太赫兹探头油液实时判断",
            font=("Microsoft YaHei", 18, "bold"),
            bg="#f3f4f6",
            fg="#111827",
        )
        title.pack(anchor="w")

        self.status_label = tk.Label(
            main,
            text="采集中",
            font=("Microsoft YaHei", 46, "bold"),
            bg="#2563eb",
            fg="white",
            padx=16,
            pady=16,
        )
        self.status_label.pack(fill=tk.X, pady=(16, 12))

        self.detail_label = tk.Label(
            main,
            text="等待第一个完整回波块...",
            font=("Microsoft YaHei", 13),
            bg="#f3f4f6",
            fg="#111827",
            justify=tk.LEFT,
        )
        self.detail_label.pack(anchor="w", fill=tk.X)

        self.position_label = tk.Label(
            main,
            text=f"串口: {self.settings.port} | 波特率: {self.settings.baudrate}",
            font=("Microsoft YaHei", 11),
            bg="#f3f4f6",
            fg="#374151",
            justify=tk.LEFT,
        )
        self.position_label.pack(anchor="w", fill=tk.X, pady=(10, 0))

        self.info_label = tk.Label(
            main,
            text="后台会每 1 秒发送 !TEST:2，并按每个完整数据块即时判断。",
            font=("Microsoft YaHei", 10),
            bg="#f3f4f6",
            fg="#4b5563",
            wraplength=510,
            justify=tk.LEFT,
        )
        self.info_label.pack(anchor="w", fill=tk.X, pady=(12, 0))

        button_row = tk.Frame(main, bg="#f3f4f6")
        button_row.pack(fill=tk.X, pady=(18, 0))

        self.stop_button = ttk.Button(button_row, text="停止采集", command=self.stop_worker)
        self.stop_button.pack(side=tk.LEFT)

        self.restart_button = ttk.Button(button_row, text="重新开始", command=self.restart_worker)
        self.restart_button.pack(side=tk.LEFT, padx=(10, 0))

        self.exit_button = ttk.Button(button_row, text="退出", command=self.close)
        self.exit_button.pack(side=tk.RIGHT)

        calibration_row = tk.Frame(main, bg="#f3f4f6")
        calibration_row.pack(fill=tk.X, pady=(12, 0))

        self.no_oil_button = ttk.Button(calibration_row, text="记录无油", command=lambda: self.set_calibration_label("无油"))
        self.no_oil_button.pack(side=tk.LEFT)

        self.oil_button = ttk.Button(calibration_row, text="记录有油", command=lambda: self.set_calibration_label("有油"))
        self.oil_button.pack(side=tk.LEFT, padx=(10, 0))

        self.stop_label_button = ttk.Button(calibration_row, text="停止标定", command=lambda: self.set_calibration_label(None))
        self.stop_label_button.pack(side=tk.LEFT, padx=(10, 0))

    def start_worker(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event = threading.Event()
        self.worker = SerialPredictionWorker(self.settings, self.output_queue, self.stop_event, self.label_state)
        self.worker.start()
        self._set_status("采集中")
        self.info_label.config(text="正在打开串口并等待数据...")

    def set_calibration_label(self, label_name: str | None) -> None:
        self.label_state.set_label(label_name)
        if label_name is None:
            self.info_label.config(text="标定记录已停止。实时判断继续运行。")
        else:
            self.info_label.config(text=f"开始记录{label_name}标定数据。确认当前真实状态就是{label_name}。")

    def stop_worker(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        self._set_status("停止中")

    def restart_worker(self) -> None:
        self.stop_worker()
        self.root.after(1200, self.start_worker)

    def close(self) -> None:
        self.stop_worker()
        self.root.after(200, self.root.destroy)

    def poll_queue(self) -> None:
        try:
            while True:
                item = self.output_queue.get_nowait()
                item_type = item.get("type")
                if item_type == "prediction":
                    self.update_prediction(item["snapshot"])  # type: ignore[arg-type]
                elif item_type == "error":
                    self._set_status("错误")
                    self.info_label.config(text=f"错误: {item.get('message')}")
                elif item_type in {"info", "stopped", "calibration"}:
                    self.info_label.config(text=str(item.get("message", "")))
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def update_prediction(self, snapshot: PredictionSnapshot) -> None:
        display_status = display_status_for_snapshot(snapshot, self.settings)
        self._set_status(display_status)
        self.detail_label.config(
            text=(
                f"快速判断: {display_status}    滚动参考: {snapshot.rolling_status}\n"
                f"P(有油): {snapshot.prob_oil:.1%}    "
                f"滚动均值: {snapshot.rolling_mean_prob:.1%}    "
                f"阳性块比例: {snapshot.positive_fraction:.1%}"
            )
        )
        self.position_label.config(
            text=(
                f"Pos: {snapshot.pos}    Dis: {snapshot.dis_mm:.2f} mm    "
                f"已用块数: {snapshot.rolling_block_count}/{self.settings.window_size}    "
                f"更新时间: {snapshot.timestamp}"
            )
        )

    def _set_status(self, status: str) -> None:
        styles = {
            "有油": ("有油", "#dc2626", "white"),
            "无油": ("无油", "#16a34a", "white"),
            "采集中": ("采集中", "#2563eb", "white"),
            "停止中": ("停止中", "#6b7280", "white"),
            "错误": ("错误", "#7f1d1d", "white"),
            "不确定-建议复测": ("不确定", "#f59e0b", "#111827"),
        }
        text, bg, fg = styles.get(status, (status, "#6b7280", "white"))
        self.status_label.config(text=text, bg=bg, fg=fg)


def main() -> None:
    root = tk.Tk()
    OilPopupApp(root, LiveSettings())
    root.mainloop()


if __name__ == "__main__":
    main()
