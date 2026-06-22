from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import joblib
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns
from scipy import stats
from scipy.signal import find_peaks, peak_widths, savgol_filter
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    roc_auc_score,
)
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


TRAIN_DIR_NAME = "训练数据"
TEST_DIR_NAME = "测试数据"
POSITIVE_LABEL_NAME = "有油"
NEGATIVE_LABEL_NAME = "无油"


FONT_FAMILY = ["Aptos", "Inter", "Segoe UI", "DejaVu Sans", "Arial", "sans-serif"]
MONO_FONT_FAMILY = ["Consolas", "DejaVu Sans Mono", "monospace"]

TOKENS = {
    "surface": "#FCFCFD",
    "panel": "#FFFFFF",
    "ink": "#1F2430",
    "muted": "#6F768A",
    "grid": "#E6E8F0",
    "axis": "#D7DBE7",
}

COLOR_FAMILIES = {
    "blue": {
        "open": TOKENS["panel"],
        "xlight": "#EAF1FE",
        "light": "#CEDFFE",
        "base": "#A3BEFA",
        "mid": "#5477C4",
        "dark": "#2E4780",
    },
    "gold": {
        "open": TOKENS["panel"],
        "xlight": "#FFF4C2",
        "light": "#FFEA8F",
        "base": "#FFE15B",
        "mid": "#B8A037",
        "dark": "#736422",
    },
    "orange": {
        "open": TOKENS["panel"],
        "xlight": "#FFEDDE",
        "light": "#FFBDA1",
        "base": "#F0986E",
        "mid": "#CC6F47",
        "dark": "#804126",
    },
    "olive": {
        "open": TOKENS["panel"],
        "xlight": "#D8ECBD",
        "light": "#BEEB96",
        "base": "#A3D576",
        "mid": "#71B436",
        "dark": "#386411",
    },
    "pink": {
        "open": TOKENS["panel"],
        "xlight": "#FCDAD6",
        "light": "#F5BACC",
        "base": "#F390CA",
        "mid": "#BD569B",
        "dark": "#8A3A6F",
    },
}


@dataclass
class Record:
    split: str
    file_path: Path
    file_id: str
    block_index: int
    pos: int
    dis_mm: float
    data: np.ndarray
    cfar: np.ndarray
    label: int | None = None
    label_name: str | None = None
    nominal_distance_cm: int | None = None


def robust_mad(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    med = np.nanmedian(values)
    mad = np.nanmedian(np.abs(values - med))
    return float(mad * 1.4826 + 1e-9)


def safe_div(num: float | np.ndarray, den: float | np.ndarray) -> float | np.ndarray:
    return np.asarray(num) / (np.asarray(den) + 1e-9)


def smooth_signal(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if values.size >= 13:
        window = 11 if values.size >= 11 else values.size // 2 * 2 + 1
        return savgol_filter(values, window_length=window, polyorder=3, mode="interp")
    return values


def parse_dat_file(path: Path, *, split: str, label: int | None = None, label_name: str | None = None,
                   nominal_distance_cm: int | None = None) -> list[Record]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    header_pattern = re.compile(
        r"Pos:\s*(?P<pos>\d+)\s*\r?\nDis:\s*(?P<dis>[\d.]+)mm\s*\r?\ndata\s*-\s*cfar",
        re.IGNORECASE,
    )
    headers = list(header_pattern.finditer(text))
    records: list[Record] = []
    for block_index, match in enumerate(headers):
        start = match.end()
        end = headers[block_index + 1].start() if block_index + 1 < len(headers) else len(text)
        pairs = re.findall(
            r"^\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\s*$",
            text[start:end],
            flags=re.MULTILINE,
        )
        if len(pairs) < 20:
            continue
        arr = np.asarray(pairs, dtype=float)
        records.append(
            Record(
                split=split,
                file_path=path,
                file_id=str(path.relative_to(path.parents[3])) if split == "train" else path.name,
                block_index=block_index,
                pos=int(match.group("pos")),
                dis_mm=float(match.group("dis")),
                data=arr[:, 0],
                cfar=arr[:, 1],
                label=label,
                label_name=label_name,
                nominal_distance_cm=nominal_distance_cm,
            )
        )
    return records


def load_records(data_root: Path) -> tuple[list[Record], list[Record]]:
    train_root = data_root / TRAIN_DIR_NAME
    test_root = data_root / TEST_DIR_NAME
    train_records: list[Record] = []
    for label_name, label in [(NEGATIVE_LABEL_NAME, 0), (POSITIVE_LABEL_NAME, 1)]:
        label_root = train_root / label_name
        for dist_dir in sorted(label_root.iterdir(), key=lambda p: int(p.name.replace("cm", ""))):
            if not dist_dir.is_dir():
                continue
            nominal_distance_cm = int(dist_dir.name.replace("cm", ""))
            for path in sorted(dist_dir.glob("*.DAT")):
                train_records.extend(
                    parse_dat_file(
                        path,
                        split="train",
                        label=label,
                        label_name=label_name,
                        nominal_distance_cm=nominal_distance_cm,
                    )
                )

    test_records: list[Record] = []
    for path in sorted(test_root.glob("*.DAT")):
        test_records.extend(parse_dat_file(path, split="test"))
    return train_records, test_records


def vector_stats(prefix: str, values: np.ndarray) -> dict[str, float]:
    values = np.asarray(values, dtype=float)
    q = np.nanquantile(values, [0.02, 0.10, 0.25, 0.50, 0.75, 0.90, 0.98])
    mean = float(np.nanmean(values))
    std = float(np.nanstd(values) + 1e-9)
    med = float(q[3])
    features = {
        f"{prefix}_mean": mean,
        f"{prefix}_std": std,
        f"{prefix}_median": med,
        f"{prefix}_mad": robust_mad(values),
        f"{prefix}_min": float(np.nanmin(values)),
        f"{prefix}_max": float(np.nanmax(values)),
        f"{prefix}_p02": float(q[0]),
        f"{prefix}_p10": float(q[1]),
        f"{prefix}_p25": float(q[2]),
        f"{prefix}_p75": float(q[4]),
        f"{prefix}_p90": float(q[5]),
        f"{prefix}_p98": float(q[6]),
        f"{prefix}_iqr": float(q[4] - q[2]),
        f"{prefix}_rms": float(np.sqrt(np.nanmean(np.square(values)))),
        f"{prefix}_abs_area": float(np.nanmean(np.abs(values))),
        f"{prefix}_crest_factor": float(np.nanmax(np.abs(values)) / (np.sqrt(np.nanmean(np.square(values))) + 1e-9)),
        f"{prefix}_skew": float(stats.skew(values, nan_policy="omit")),
        f"{prefix}_kurtosis": float(stats.kurtosis(values, fisher=True, nan_policy="omit")),
    }
    if values.size > 2:
        centered = values - mean
        denom = float(np.sum(centered * centered) + 1e-9)
        features[f"{prefix}_autocorr1"] = float(np.sum(centered[:-1] * centered[1:]) / denom)
        features[f"{prefix}_total_variation"] = float(np.nanmean(np.abs(np.diff(values))))
        features[f"{prefix}_diff_std"] = float(np.nanstd(np.diff(values)))
    return features


def entropy_from_positive(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    positive = values - np.nanmin(values)
    total = np.nansum(positive)
    if total <= 1e-12:
        return 0.0
    probs = positive / total
    probs = probs[probs > 0]
    return float(-(probs * np.log2(probs)).sum() / np.log2(values.size))


def spectral_features(values: np.ndarray) -> dict[str, float]:
    values = np.asarray(values, dtype=float)
    centered = values - np.nanmean(values)
    windowed = centered * np.hanning(values.size)
    power = np.abs(np.fft.rfft(windowed)) ** 2
    power[0] = 0.0
    total = float(np.sum(power) + 1e-9)
    freqs = np.linspace(0, 0.5, power.size)
    features = {
        "spec_entropy": entropy_from_positive(power),
        "spec_centroid": float(np.sum(freqs * power) / total),
        "spec_bandwidth": float(np.sqrt(np.sum(((freqs - np.sum(freqs * power) / total) ** 2) * power) / total)),
    }
    bands = [(0.00, 0.05), (0.05, 0.12), (0.12, 0.25), (0.25, 0.50)]
    for lo, hi in bands:
        mask = (freqs >= lo) & (freqs < hi)
        features[f"spec_energy_{lo:.2f}_{hi:.2f}"] = float(np.sum(power[mask]) / total)
    return features


def peak_features(values: np.ndarray, cfar: np.ndarray) -> dict[str, float]:
    values = np.asarray(values, dtype=float)
    cfar = np.asarray(cfar, dtype=float)
    n = values.size
    med = float(np.nanmedian(values))
    mad = robust_mad(values)
    z = (values - med) / mad
    smooth_z = smooth_signal(z)
    peaks, properties = find_peaks(smooth_z, prominence=1.0, distance=max(2, n // 40))
    prominences = properties.get("prominences", np.array([], dtype=float))
    if peaks.size:
        order = np.argsort(smooth_z[peaks])[::-1]
        peaks = peaks[order]
        prominences = prominences[order]
        widths = peak_widths(smooth_z, peaks, rel_height=0.5)[0] / n
    else:
        widths = np.array([], dtype=float)

    features: dict[str, float] = {
        "peak_count_prom1": float(peaks.size),
        "peak_density_prom1": float(peaks.size / n),
        "peak_prominence_sum": float(np.sum(prominences)) if prominences.size else 0.0,
        "peak_prominence_mean": float(np.mean(prominences)) if prominences.size else 0.0,
        "peak_prominence_max": float(np.max(prominences)) if prominences.size else 0.0,
        "peak_width_mean_rel": float(np.mean(widths)) if widths.size else 0.0,
        "peak_width_max_rel": float(np.max(widths)) if widths.size else 0.0,
    }
    top_values: list[float] = []
    for i in range(5):
        if i < peaks.size:
            idx = int(peaks[i])
            val = float(values[idx])
            top_values.append(float(smooth_z[idx]))
            features[f"peak_top{i+1}_idx_rel"] = float(idx / (n - 1))
            features[f"peak_top{i+1}_z"] = float(smooth_z[idx])
            features[f"peak_top{i+1}_raw"] = val
            features[f"peak_top{i+1}_prominence"] = float(prominences[i]) if i < prominences.size else 0.0
            features[f"peak_top{i+1}_cfar_margin_z"] = float((values[idx] - cfar[idx]) / (robust_mad(values - cfar)))
            features[f"peak_top{i+1}_data_over_cfar"] = float(values[idx] / (cfar[idx] + 1e-9))
        else:
            features[f"peak_top{i+1}_idx_rel"] = np.nan
            features[f"peak_top{i+1}_z"] = np.nan
            features[f"peak_top{i+1}_raw"] = np.nan
            features[f"peak_top{i+1}_prominence"] = np.nan
            features[f"peak_top{i+1}_cfar_margin_z"] = np.nan
            features[f"peak_top{i+1}_data_over_cfar"] = np.nan

    for i in range(1, 5):
        numerator = features.get(f"peak_top{i+1}_z", np.nan)
        denominator = features.get("peak_top1_z", np.nan)
        features[f"peak_top{i+1}_to_top1_ratio"] = float(safe_div(numerator, denominator)) if np.isfinite(numerator) else np.nan

    if peaks.size >= 2:
        sorted_positions = np.sort(peaks[: min(5, peaks.size)]) / (n - 1)
        gaps = np.diff(sorted_positions)
        features["peak_top_spacing_mean_rel"] = float(np.mean(gaps))
        features["peak_top_spacing_std_rel"] = float(np.std(gaps))
        features["peak_top_spacing_min_rel"] = float(np.min(gaps))
        features["peak_top_spacing_max_rel"] = float(np.max(gaps))
        features["peak_top1_top2_gap_rel"] = float(abs(peaks[0] - peaks[1]) / (n - 1))
    else:
        features["peak_top_spacing_mean_rel"] = np.nan
        features["peak_top_spacing_std_rel"] = np.nan
        features["peak_top_spacing_min_rel"] = np.nan
        features["peak_top_spacing_max_rel"] = np.nan
        features["peak_top1_top2_gap_rel"] = np.nan

    return features


def cfar_features(data: np.ndarray, cfar: np.ndarray) -> dict[str, float]:
    data = np.asarray(data, dtype=float)
    cfar = np.asarray(cfar, dtype=float)
    residual = data - cfar
    residual_mad = robust_mad(residual)
    positive = np.maximum(residual, 0.0)
    exceed = residual > 0
    if np.any(exceed):
        exceed_indices = np.flatnonzero(exceed)
        first_rel = float(exceed_indices[0] / (data.size - 1))
        last_rel = float(exceed_indices[-1] / (data.size - 1))
    else:
        first_rel = np.nan
        last_rel = np.nan

    features = {
        "cfar_corr_data": float(np.corrcoef(data, cfar)[0, 1]) if np.nanstd(data) > 0 and np.nanstd(cfar) > 0 else 0.0,
        "cfar_mean": float(np.nanmean(cfar)),
        "cfar_std": float(np.nanstd(cfar)),
        "cfar_mad": robust_mad(cfar),
        "cfar_residual_mean": float(np.nanmean(residual)),
        "cfar_residual_std": float(np.nanstd(residual)),
        "cfar_residual_max": float(np.nanmax(residual)),
        "cfar_residual_mad": residual_mad,
        "cfar_exceed_count": float(np.sum(exceed)),
        "cfar_exceed_frac": float(np.mean(exceed)),
        "cfar_exceed_area": float(np.mean(positive)),
        "cfar_exceed_area_z": float(np.mean(positive) / residual_mad),
        "cfar_max_margin_z": float(np.nanmax(residual) / residual_mad),
        "cfar_first_exceed_idx_rel": first_rel,
        "cfar_last_exceed_idx_rel": last_rel,
        "cfar_exceed_span_rel": float(last_rel - first_rel) if np.isfinite(first_rel) and np.isfinite(last_rel) else np.nan,
        "cfar_data_over_threshold_mean": float(np.nanmean(data / (cfar + 1e-9))),
        "cfar_data_over_threshold_max": float(np.nanmax(data / (cfar + 1e-9))),
    }
    return features


def window_features(data: np.ndarray, cfar: np.ndarray, bins: int = 5) -> dict[str, float]:
    data = np.asarray(data, dtype=float)
    cfar = np.asarray(cfar, dtype=float)
    n = data.size
    med = float(np.nanmedian(data))
    mad = robust_mad(data)
    z = (data - med) / mad
    residual_pos = np.maximum(data - cfar, 0.0)
    total_energy = float(np.sum(z * z) + 1e-9)
    total_residual = float(np.sum(residual_pos) + 1e-9)
    features: dict[str, float] = {}
    indices = np.array_split(np.arange(n), bins)
    for i, idx in enumerate(indices, start=1):
        part = z[idx]
        raw_part = data[idx]
        res_part = residual_pos[idx]
        peaks, _ = find_peaks(smooth_signal(part), prominence=1.0, distance=max(2, len(part) // 10))
        features[f"win{i}_z_max"] = float(np.nanmax(part))
        features[f"win{i}_raw_max"] = float(np.nanmax(raw_part))
        features[f"win{i}_energy_share"] = float(np.sum(part * part) / total_energy)
        features[f"win{i}_residual_share"] = float(np.sum(res_part) / total_residual)
        features[f"win{i}_residual_mean_z"] = float(np.mean(res_part) / (robust_mad(data - cfar)))
        features[f"win{i}_peak_count"] = float(peaks.size)

    features["win_late_to_early_energy"] = float(safe_div(features["win5_energy_share"], features["win1_energy_share"]))
    features["win_mid_to_early_energy"] = float(safe_div(features["win3_energy_share"], features["win1_energy_share"]))
    features["win_late_to_early_residual"] = float(safe_div(features["win5_residual_share"], features["win1_residual_share"]))
    features["win_front_back_max_z_ratio"] = float(safe_div(features["win5_z_max"], features["win1_z_max"]))
    return features


def extract_features(record: Record) -> dict[str, float | str | int | None]:
    data = record.data.astype(float)
    cfar = record.cfar.astype(float)
    n = data.size
    med = float(np.nanmedian(data))
    mad = robust_mad(data)
    z = (data - med) / mad
    cfar_med = float(np.nanmedian(cfar))
    cfar_mad = robust_mad(cfar)
    cfar_z = (cfar - cfar_med) / cfar_mad
    dis_m = record.dis_mm / 1000.0 if record.dis_mm and record.dis_mm > 0 else np.nan

    features: dict[str, float | str | int | None] = {
        "split": record.split,
        "file_id": record.file_id,
        "file_name": record.file_path.name,
        "relative_path": str(record.file_path),
        "block_index": record.block_index,
        "label": record.label,
        "label_name": record.label_name,
        "nominal_distance_cm": record.nominal_distance_cm,
        "meta_pos": record.pos,
        "meta_dis_mm": record.dis_mm,
        "n_samples": n,
    }
    features.update(vector_stats("raw", data))
    features.update(vector_stats("z", z))
    features.update(vector_stats("cfar_z", cfar_z))
    features.update(cfar_features(data, cfar))
    features.update(peak_features(data, cfar))
    features.update(window_features(data, cfar))
    features.update(spectral_features(z))
    features["shape_entropy"] = entropy_from_positive(z)
    features["distnorm_raw_max_times_r2"] = float(np.nanmax(data) * dis_m * dis_m) if np.isfinite(dis_m) else np.nan
    features["distnorm_raw_rms_times_r2"] = float(np.sqrt(np.nanmean(data * data)) * dis_m * dis_m) if np.isfinite(dis_m) else np.nan
    features["distnorm_cfar_margin_times_r2"] = float(np.nanmax(data - cfar) * dis_m * dis_m) if np.isfinite(dis_m) else np.nan
    return features


def build_feature_frame(records: Iterable[Record]) -> pd.DataFrame:
    return pd.DataFrame([extract_features(record) for record in records])


def numeric_feature_columns(df: pd.DataFrame, *, include_metadata: bool) -> list[str]:
    exclude = {
        "split",
        "file_id",
        "file_name",
        "relative_path",
        "block_index",
        "label",
        "label_name",
        "nominal_distance_cm",
    }
    if not include_metadata:
        exclude.update({"meta_pos", "meta_dis_mm"})
    columns: list[str] = []
    for column in df.columns:
        if column in exclude:
            continue
        if pd.api.types.is_numeric_dtype(df[column]):
            columns.append(column)
    return columns


def make_models(random_state: int) -> dict[str, Pipeline]:
    return {
        "logistic_l2": Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                (
                    "model",
                    LogisticRegression(
                        penalty="l2",
                        solver="lbfgs",
                        C=0.6,
                        max_iter=2000,
                        class_weight="balanced",
                        random_state=random_state,
                    ),
                ),
            ]
        ),
        "random_forest": Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                (
                    "model",
                    RandomForestClassifier(
                        n_estimators=300,
                        min_samples_leaf=8,
                        class_weight="balanced_subsample",
                        random_state=random_state,
                        n_jobs=-1,
                    ),
                ),
            ]
        ),
        "extra_trees": Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                (
                    "model",
                    ExtraTreesClassifier(
                        n_estimators=400,
                        min_samples_leaf=6,
                        class_weight="balanced",
                        random_state=random_state,
                        n_jobs=-1,
                    ),
                ),
            ]
        ),
        "hist_gradient_boosting": Pipeline(
            [
                ("imputer", SimpleImputer(strategy="median")),
                (
                    "model",
                    HistGradientBoostingClassifier(
                        learning_rate=0.06,
                        max_iter=180,
                        l2_regularization=0.02,
                        random_state=random_state,
                    ),
                ),
            ]
        ),
    }


def predict_proba_positive(model: Pipeline, x: pd.DataFrame) -> np.ndarray:
    proba = model.predict_proba(x)
    return proba[:, list(model.classes_).index(1)] if hasattr(model, "classes_") else proba[:, 1]


def optimize_threshold(y_true: np.ndarray, proba: np.ndarray) -> tuple[float, float]:
    thresholds = np.linspace(0.05, 0.95, 181)
    best_threshold = 0.5
    best_score = -np.inf
    for threshold in thresholds:
        pred = (proba >= threshold).astype(int)
        score = balanced_accuracy_score(y_true, pred)
        if score > best_score:
            best_score = score
            best_threshold = float(threshold)
    return best_threshold, float(best_score)


def file_level_predictions(meta: pd.DataFrame, proba: np.ndarray) -> pd.DataFrame:
    temp = meta.copy()
    temp["prob_oil"] = proba
    grouped = (
        temp.groupby("file_id")
        .agg(
            label=("label", "first"),
            label_name=("label_name", "first"),
            nominal_distance_cm=("nominal_distance_cm", "first"),
            block_count=("block_index", "count"),
            prob_oil_mean=("prob_oil", "mean"),
            prob_oil_median=("prob_oil", "median"),
            prob_oil_std=("prob_oil", "std"),
        )
        .reset_index()
    )
    grouped["prob_oil_std"] = grouped["prob_oil_std"].fillna(0.0)
    return grouped


def evaluate_group_cv(
    df: pd.DataFrame,
    feature_columns: list[str],
    models: dict[str, Pipeline],
    *,
    random_state: int,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    y = df["label"].astype(int).to_numpy()
    groups = df["file_id"].to_numpy()
    n_splits = min(5, len(np.unique(groups)))
    splitter = GroupKFold(n_splits=n_splits)
    rows: list[dict[str, float | str]] = []
    file_pred_tables: dict[str, pd.DataFrame] = {}
    block_pred_tables: list[pd.DataFrame] = []

    for model_name, model in models.items():
        oof_proba = np.full(len(df), np.nan)
        fold_rows: list[dict[str, float | str | int]] = []
        for fold, (train_idx, valid_idx) in enumerate(splitter.split(df[feature_columns], y, groups), start=1):
            x_train = df.iloc[train_idx][feature_columns]
            y_train = y[train_idx]
            x_valid = df.iloc[valid_idx][feature_columns]
            model.fit(x_train, y_train)
            proba = predict_proba_positive(model, x_valid)
            oof_proba[valid_idx] = proba
            fold_rows.append(
                {
                    "model": model_name,
                    "fold": fold,
                    "valid_files": len(np.unique(groups[valid_idx])),
                    "block_auc": roc_auc_score(y[valid_idx], proba),
                    "block_accuracy_0p5": accuracy_score(y[valid_idx], proba >= 0.5),
                }
            )

        file_preds = file_level_predictions(
            df[["file_id", "label", "label_name", "nominal_distance_cm", "block_index"]], oof_proba
        )
        threshold, threshold_bal_acc = optimize_threshold(file_preds["label"].to_numpy(), file_preds["prob_oil_mean"].to_numpy())
        file_preds["pred_label"] = (file_preds["prob_oil_mean"] >= threshold).astype(int)
        file_preds["pred_name"] = np.where(file_preds["pred_label"] == 1, POSITIVE_LABEL_NAME, NEGATIVE_LABEL_NAME)
        file_preds["model"] = model_name
        file_preds["threshold"] = threshold
        file_pred_tables[model_name] = file_preds

        block_threshold, _ = optimize_threshold(y, oof_proba)
        block_pred = df[["file_id", "block_index", "label", "label_name", "nominal_distance_cm"]].copy()
        block_pred["model"] = model_name
        block_pred["prob_oil"] = oof_proba
        block_pred["pred_label"] = (oof_proba >= block_threshold).astype(int)
        block_pred_tables.append(block_pred)

        rows.append(
            {
                "model": model_name,
                "feature_count": len(feature_columns),
                "block_auc": roc_auc_score(y, oof_proba),
                "block_accuracy_0p5": accuracy_score(y, oof_proba >= 0.5),
                "block_balanced_accuracy_opt": balanced_accuracy_score(y, oof_proba >= block_threshold),
                "block_threshold": block_threshold,
                "file_auc": roc_auc_score(file_preds["label"], file_preds["prob_oil_mean"]),
                "file_accuracy_opt": accuracy_score(file_preds["label"], file_preds["pred_label"]),
                "file_balanced_accuracy_opt": threshold_bal_acc,
                "file_f1_opt": f1_score(file_preds["label"], file_preds["pred_label"]),
                "file_threshold": threshold,
            }
        )

    return pd.DataFrame(rows).sort_values(["file_balanced_accuracy_opt", "file_auc", "block_auc"], ascending=False), pd.concat(
        block_pred_tables, ignore_index=True
    ), file_pred_tables


def evaluate_leave_distance_out(
    df: pd.DataFrame,
    feature_columns: list[str],
    model: Pipeline,
    *,
    deployment_threshold: float,
) -> pd.DataFrame:
    rows: list[dict[str, float | int]] = []
    y_all = df["label"].astype(int).to_numpy()
    for distance in sorted(df["nominal_distance_cm"].dropna().unique()):
        train_mask = df["nominal_distance_cm"] != distance
        valid_mask = df["nominal_distance_cm"] == distance
        if len(np.unique(y_all[train_mask])) < 2 or len(np.unique(y_all[valid_mask])) < 2:
            continue
        model.fit(df.loc[train_mask, feature_columns], y_all[train_mask])
        proba = predict_proba_positive(model, df.loc[valid_mask, feature_columns])
        file_preds = file_level_predictions(
            df.loc[valid_mask, ["file_id", "label", "label_name", "nominal_distance_cm", "block_index"]],
            proba,
        )
        file_preds["pred_label"] = (file_preds["prob_oil_mean"] >= deployment_threshold).astype(int)
        file_preds["pred_label_0p5"] = (file_preds["prob_oil_mean"] >= 0.5).astype(int)
        rows.append(
            {
                "held_out_distance_cm": int(distance),
                "deployment_threshold": deployment_threshold,
                "block_count": int(valid_mask.sum()),
                "file_count": int(file_preds.shape[0]),
                "block_auc": roc_auc_score(y_all[valid_mask], proba),
                "block_accuracy_0p5": accuracy_score(y_all[valid_mask], proba >= 0.5),
                "file_auc": roc_auc_score(file_preds["label"], file_preds["prob_oil_mean"]),
                "file_accuracy_at_threshold": accuracy_score(file_preds["label"], file_preds["pred_label"]),
                "file_balanced_accuracy_at_threshold": balanced_accuracy_score(file_preds["label"], file_preds["pred_label"]),
                "file_accuracy_0p5": accuracy_score(file_preds["label"], file_preds["pred_label_0p5"]),
                "file_balanced_accuracy_0p5": balanced_accuracy_score(file_preds["label"], file_preds["pred_label_0p5"]),
            }
        )
    return pd.DataFrame(rows)


def final_model_feature_importance(model: Pipeline, feature_columns: list[str]) -> pd.DataFrame:
    estimator = model.named_steps["model"]
    if hasattr(estimator, "feature_importances_"):
        importance = estimator.feature_importances_
    elif hasattr(estimator, "coef_"):
        importance = np.abs(estimator.coef_).ravel()
    else:
        importance = np.full(len(feature_columns), np.nan)
    return (
        pd.DataFrame({"feature": feature_columns, "importance": importance})
        .sort_values("importance", ascending=False)
        .reset_index(drop=True)
    )


def use_chart_theme() -> None:
    sns.set_theme(
        style="whitegrid",
        rc={
            "figure.facecolor": TOKENS["surface"],
            "figure.edgecolor": "none",
            "savefig.facecolor": TOKENS["surface"],
            "savefig.edgecolor": "none",
            "axes.facecolor": TOKENS["panel"],
            "axes.edgecolor": TOKENS["axis"],
            "axes.labelcolor": TOKENS["ink"],
            "axes.grid": True,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "grid.color": TOKENS["grid"],
            "grid.linewidth": 0.8,
            "font.family": "sans-serif",
            "font.sans-serif": FONT_FAMILY,
            "font.monospace": MONO_FONT_FAMILY,
            "patch.linewidth": 1.0,
        },
    )


def add_chart_header(fig, ax, title: str, subtitle: str) -> None:
    ax.set_title("")
    fig.subplots_adjust(top=0.78)
    left = ax.get_position().x0
    fig.text(left, 0.965, title, ha="left", va="top", fontsize=13, fontweight="semibold", color=TOKENS["ink"])
    fig.text(left, 0.915, subtitle, ha="left", va="top", fontsize=9, color=TOKENS["muted"])
    sns.despine(ax=ax)


def save_model_comparison_chart(model_scores: pd.DataFrame, output_dir: Path) -> Path:
    use_chart_theme()
    chart_path = output_dir / "charts" / "model_comparison.png"
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    plot_df = model_scores.sort_values("file_balanced_accuracy_opt", ascending=True)
    fig, ax = plt.subplots(figsize=(8.5, 5.2), dpi=180)
    family = COLOR_FAMILIES["blue"]
    sns.barplot(
        data=plot_df,
        x="file_balanced_accuracy_opt",
        y="model",
        ax=ax,
        color=family["base"],
        edgecolor=family["dark"],
        linewidth=1.0,
    )
    ax.set_xlabel("File-level balanced accuracy")
    ax.set_ylabel("")
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    ax.set_xlim(0, 1.02)
    add_chart_header(fig, ax, "Grouped validation by source file", "Model selection used file-grouped folds; repeated blocks from one file never crossed folds.")
    fig.savefig(chart_path, bbox_inches="tight")
    plt.close(fig)
    return chart_path


def save_distance_validation_chart(distance_scores: pd.DataFrame, output_dir: Path) -> Path:
    use_chart_theme()
    chart_path = output_dir / "charts" / "distance_validation.png"
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8.5, 5.2), dpi=180)
    family = COLOR_FAMILIES["gold"]
    plot_df = distance_scores.copy()
    plot_df["held_out_distance_cm"] = plot_df["held_out_distance_cm"].astype(str)
    sns.barplot(
        data=plot_df,
        x="held_out_distance_cm",
        y="file_balanced_accuracy_at_threshold",
        ax=ax,
        color=family["base"],
        edgecolor=family["dark"],
        linewidth=1.0,
    )
    ax.set_xlabel("Held-out nominal distance (cm)")
    ax.set_ylabel("File-level balanced accuracy")
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    ax.set_ylim(0, 1.02)
    add_chart_header(
        fig,
        ax,
        "Leave-one-distance validation",
        "Each bar trains on five distances and tests on the held-out distance with the fixed deployment threshold.",
    )
    fig.savefig(chart_path, bbox_inches="tight")
    plt.close(fig)
    return chart_path


def save_feature_importance_chart(importance: pd.DataFrame, output_dir: Path, top_n: int = 20) -> Path:
    use_chart_theme()
    chart_path = output_dir / "charts" / "feature_importance.png"
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    plot_df = importance.head(top_n).sort_values("importance", ascending=True)
    fig, ax = plt.subplots(figsize=(8.5, 7.2), dpi=180)
    family = COLOR_FAMILIES["orange"]
    sns.barplot(
        data=plot_df,
        x="importance",
        y="feature",
        ax=ax,
        color=family["base"],
        edgecolor=family["dark"],
        linewidth=1.0,
    )
    ax.set_xlabel("Model importance")
    ax.set_ylabel("")
    add_chart_header(fig, ax, "Most influential engineered features", "Importance is from the final fitted model on training data only.")
    fig.savefig(chart_path, bbox_inches="tight")
    plt.close(fig)
    return chart_path


def save_test_prediction_chart(test_file_predictions: pd.DataFrame, output_dir: Path, threshold: float) -> Path:
    use_chart_theme()
    chart_path = output_dir / "charts" / "test_predictions.png"
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    plot_df = test_file_predictions.sort_values("prob_oil_mean", ascending=True)
    fig, ax = plt.subplots(figsize=(8.5, 4.8), dpi=180)
    family = COLOR_FAMILIES["olive"]
    sns.barplot(
        data=plot_df,
        x="prob_oil_mean",
        y="file_id",
        ax=ax,
        color=family["base"],
        edgecolor=family["dark"],
        linewidth=1.0,
    )
    ax.axvline(threshold, color=TOKENS["ink"], linestyle=":", linewidth=1.0, label=f"Threshold {threshold:.2f}")
    ax.set_xlabel("Predicted probability of oil")
    ax.set_ylabel("")
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(1.0))
    ax.set_xlim(0, 1.02)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.02), frameon=False)
    add_chart_header(fig, ax, "Held-out test file predictions", "Test files were not used in feature selection, validation, thresholding, or final fitting.")
    fig.savefig(chart_path, bbox_inches="tight")
    plt.close(fig)
    return chart_path


def save_example_waveform_chart(train_df: pd.DataFrame, train_records: list[Record], output_dir: Path) -> Path:
    use_chart_theme()
    chart_path = output_dir / "charts" / "example_waveforms.png"
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    examples = []
    for label in [0, 1]:
        subset = train_df[train_df["label"] == label]
        file_id = subset.groupby("file_id").size().sort_values(ascending=False).index[0]
        row = subset[subset["file_id"] == file_id].iloc[0]
        record = next(
            rec
            for rec in train_records
            if rec.file_id == row["file_id"] and rec.block_index == int(row["block_index"])
        )
        examples.append(record)

    fig, axes = plt.subplots(2, 1, figsize=(8.5, 6.2), dpi=180, sharex=True)
    family_map = {0: COLOR_FAMILIES["blue"], 1: COLOR_FAMILIES["orange"]}
    for ax, record in zip(axes, examples):
        x = np.arange(record.data.size)
        family = family_map[int(record.label or 0)]
        ax.plot(x, record.data, color=family["base"], linewidth=1.0, label="data")
        ax.plot(x, record.cfar, color=TOKENS["muted"], linestyle="--", linewidth=1.0, label="cfar")
        ax.fill_between(x, record.cfar, record.data, where=record.data > record.cfar, color=family["xlight"], alpha=0.6)
        ax.set_ylabel("Oil" if record.label == 1 else "No oil")
        ax.legend(loc="upper right", frameon=False, ncol=2)
    axes[-1].set_xlabel("Sample index")
    add_chart_header(fig, axes[0], "Example data and CFAR traces", "Features use peak structure, residual area, windows, spectra, and robust normalized amplitudes.")
    fig.savefig(chart_path, bbox_inches="tight")
    plt.close(fig)
    return chart_path


def aggregate_test_predictions(test_df: pd.DataFrame, proba: np.ndarray, threshold: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    block_predictions = test_df[["file_id", "file_name", "relative_path", "block_index", "meta_pos", "meta_dis_mm"]].copy()
    block_predictions["prob_oil"] = proba
    block_predictions["pred_label"] = (block_predictions["prob_oil"] >= threshold).astype(int)
    block_predictions["pred_name"] = np.where(block_predictions["pred_label"] == 1, POSITIVE_LABEL_NAME, NEGATIVE_LABEL_NAME)
    file_predictions = (
        block_predictions.groupby("file_id")
        .agg(
            block_count=("block_index", "count"),
            pos_values=("meta_pos", lambda x: ",".join(map(str, sorted(set(map(int, x)))))),
            dis_mm_values=("meta_dis_mm", lambda x: ",".join(f"{v:.2f}" for v in sorted(set(x)))),
            prob_oil_mean=("prob_oil", "mean"),
            prob_oil_median=("prob_oil", "median"),
            prob_oil_std=("prob_oil", "std"),
            positive_block_frac=("pred_label", "mean"),
        )
        .reset_index()
    )
    file_predictions["prob_oil_std"] = file_predictions["prob_oil_std"].fillna(0.0)
    file_predictions["pred_label"] = (file_predictions["prob_oil_mean"] >= threshold).astype(int)
    file_predictions["pred_name"] = np.where(file_predictions["pred_label"] == 1, POSITIVE_LABEL_NAME, NEGATIVE_LABEL_NAME)
    file_predictions["threshold"] = threshold
    return block_predictions, file_predictions


def confusion_matrix_table(file_preds: pd.DataFrame) -> dict[str, int]:
    matrix = confusion_matrix(file_preds["label"], file_preds["pred_label"], labels=[0, 1])
    return {
        "true_no_oil_pred_no_oil": int(matrix[0, 0]),
        "true_no_oil_pred_oil": int(matrix[0, 1]),
        "true_oil_pred_no_oil": int(matrix[1, 0]),
        "true_oil_pred_oil": int(matrix[1, 1]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train an oil/no-oil classifier from terahertz DAT traces.")
    parser.add_argument("--data-root", type=Path, default=Path("shuju"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()

    data_root = args.data_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    train_records, test_records = load_records(data_root)
    train_df = build_feature_frame(train_records)
    test_df = build_feature_frame(test_records)

    train_df.to_csv(output_dir / "train_features.csv", index=False, encoding="utf-8-sig")
    test_df.to_csv(output_dir / "test_features.csv", index=False, encoding="utf-8-sig")

    feature_sets = {
        "stable_no_raw_metadata": numeric_feature_columns(train_df, include_metadata=False),
        "with_position_distance_metadata": numeric_feature_columns(train_df, include_metadata=True),
    }
    models = make_models(args.random_state)
    all_scores: list[pd.DataFrame] = []
    all_block_preds: list[pd.DataFrame] = []
    all_file_preds: dict[str, pd.DataFrame] = {}

    for feature_set_name, feature_columns in feature_sets.items():
        scores, block_preds, file_pred_tables = evaluate_group_cv(
            train_df,
            feature_columns,
            make_models(args.random_state),
            random_state=args.random_state,
        )
        scores.insert(0, "feature_set", feature_set_name)
        block_preds.insert(0, "feature_set", feature_set_name)
        all_scores.append(scores)
        all_block_preds.append(block_preds)
        for model_name, table in file_pred_tables.items():
            key = f"{feature_set_name}__{model_name}"
            temp = table.copy()
            temp.insert(0, "feature_set", feature_set_name)
            all_file_preds[key] = temp

    score_df = pd.concat(all_scores, ignore_index=True).sort_values(
        ["file_balanced_accuracy_opt", "file_auc", "block_auc"],
        ascending=False,
    )
    score_df.to_csv(output_dir / "model_cv_scores.csv", index=False, encoding="utf-8-sig")
    pd.concat(all_block_preds, ignore_index=True).to_csv(output_dir / "oof_block_predictions.csv", index=False, encoding="utf-8-sig")
    pd.concat(all_file_preds.values(), ignore_index=True).to_csv(output_dir / "oof_file_predictions.csv", index=False, encoding="utf-8-sig")

    stable_scores = score_df[score_df["feature_set"] == "stable_no_raw_metadata"].copy()
    selected_row = stable_scores.iloc[0] if not stable_scores.empty else score_df.iloc[0]
    selected_feature_set = str(selected_row["feature_set"])
    selected_model_name = str(selected_row["model"])
    selected_features = feature_sets[selected_feature_set]
    selected_model = make_models(args.random_state)[selected_model_name]

    selected_oof_key = f"{selected_feature_set}__{selected_model_name}"
    selected_file_oof = all_file_preds[selected_oof_key].copy()
    selected_threshold = float(selected_file_oof["threshold"].iloc[0])

    distance_scores = evaluate_leave_distance_out(
        train_df,
        selected_features,
        make_models(args.random_state)[selected_model_name],
        deployment_threshold=selected_threshold,
    )
    distance_scores.to_csv(output_dir / "leave_distance_out_scores.csv", index=False, encoding="utf-8-sig")

    y_train = train_df["label"].astype(int).to_numpy()
    selected_model.fit(train_df[selected_features], y_train)
    train_file_probs = predict_proba_positive(selected_model, train_df[selected_features])
    train_file_fit = file_level_predictions(
        train_df[["file_id", "label", "label_name", "nominal_distance_cm", "block_index"]],
        train_file_probs,
    )
    train_file_fit["pred_label"] = (train_file_fit["prob_oil_mean"] >= selected_threshold).astype(int)
    train_file_fit["pred_name"] = np.where(train_file_fit["pred_label"] == 1, POSITIVE_LABEL_NAME, NEGATIVE_LABEL_NAME)
    train_file_fit.to_csv(output_dir / "train_fit_file_predictions.csv", index=False, encoding="utf-8-sig")

    test_proba = predict_proba_positive(selected_model, test_df[selected_features])
    test_block_predictions, test_file_predictions = aggregate_test_predictions(test_df, test_proba, selected_threshold)
    test_block_predictions.to_csv(output_dir / "test_block_predictions.csv", index=False, encoding="utf-8-sig")
    test_file_predictions.to_csv(output_dir / "test_file_predictions.csv", index=False, encoding="utf-8-sig")

    model_path = output_dir / "oil_classifier_model.joblib"
    joblib.dump(
        {
            "model": selected_model,
            "feature_columns": selected_features,
            "threshold": selected_threshold,
            "selected_model_name": selected_model_name,
            "selected_feature_set": selected_feature_set,
            "positive_label": POSITIVE_LABEL_NAME,
            "negative_label": NEGATIVE_LABEL_NAME,
        },
        model_path,
    )

    importance = final_model_feature_importance(selected_model, selected_features)
    importance.to_csv(output_dir / "feature_importance.csv", index=False, encoding="utf-8-sig")

    chart_paths = {
        "model_comparison": str(save_model_comparison_chart(stable_scores if not stable_scores.empty else score_df, output_dir)),
        "distance_validation": str(save_distance_validation_chart(distance_scores, output_dir)),
        "feature_importance": str(save_feature_importance_chart(importance, output_dir)),
        "test_predictions": str(save_test_prediction_chart(test_file_predictions, output_dir, selected_threshold)),
        "example_waveforms": str(save_example_waveform_chart(train_df, train_records, output_dir)),
    }

    feature_categories = {
        "robust_amplitude_shape": int(sum(col.startswith(("raw_", "z_", "shape_")) for col in selected_features)),
        "cfar_threshold_relation": int(sum(col.startswith("cfar_") for col in selected_features)),
        "multi_peak_structure": int(sum(col.startswith("peak_") for col in selected_features)),
        "window_multipath": int(sum(col.startswith("win") for col in selected_features)),
        "spectral": int(sum(col.startswith("spec_") for col in selected_features)),
        "distance_normalized": int(sum(col.startswith("distnorm_") for col in selected_features)),
        "metadata_used": int(sum(col.startswith("meta_") for col in selected_features)),
    }

    summary = {
        "data_root": str(data_root),
        "output_dir": str(output_dir),
        "train_files": int(train_df["file_id"].nunique()),
        "train_blocks": int(train_df.shape[0]),
        "test_files": int(test_df["file_id"].nunique()),
        "test_blocks": int(test_df.shape[0]),
        "selected_feature_set": selected_feature_set,
        "selected_model_name": selected_model_name,
        "selected_threshold": selected_threshold,
        "selected_feature_count": len(selected_features),
        "feature_categories": feature_categories,
        "selected_cv": selected_row.to_dict(),
        "selected_oof_confusion_matrix": confusion_matrix_table(selected_file_oof),
        "distance_cv_mean_file_balanced_accuracy": float(distance_scores["file_balanced_accuracy_at_threshold"].mean()) if not distance_scores.empty else None,
        "distance_cv_min_file_balanced_accuracy": float(distance_scores["file_balanced_accuracy_at_threshold"].min()) if not distance_scores.empty else None,
        "model_path": str(model_path),
        "chart_paths": chart_paths,
        "notes": [
            "Training and test folders are parsed separately.",
            "Test data are used only after final model and threshold selection.",
            "Validation is grouped by source file to prevent blocks from the same acquisition crossing folds.",
            "Leave-one-distance-out validation checks robustness under nominal distance changes.",
            "Test files are unlabeled, so test-set accuracy cannot be computed from provided data.",
        ],
    }
    (output_dir / "training_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
