from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix, roc_auc_score


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.train_oil_classifier import (
    NEGATIVE_LABEL_NAME,
    POSITIVE_LABEL_NAME,
    build_feature_frame,
    make_models,
    numeric_feature_columns,
    parse_dat_file,
    predict_proba_positive,
)


LABELS = [(NEGATIVE_LABEL_NAME, 0), (POSITIVE_LABEL_NAME, 1)]


def load_calibration_records(calibration_root: Path):
    records = []
    for label_name, label in LABELS:
        label_dir = calibration_root / label_name
        if not label_dir.exists():
            continue
        for path in sorted(label_dir.glob("*.DAT")):
            parsed = parse_dat_file(path, split="calibration", label=label, label_name=label_name)
            for record in parsed:
                record.file_id = str(path.relative_to(calibration_root))
                record.nominal_distance_cm = None
            records.extend(parsed)
    return records


def safe_auc(y_true: np.ndarray, proba: np.ndarray) -> float:
    if len(np.unique(y_true)) < 2:
        return float("nan")
    return float(roc_auc_score(y_true, proba))


def choose_low_false_positive_threshold(
    y_true: np.ndarray,
    proba: np.ndarray,
    *,
    max_false_positive_rate: float,
) -> dict[str, float]:
    thresholds = np.linspace(0.05, 0.995, 190)
    rows = []
    for threshold in thresholds:
        pred = (proba >= threshold).astype(int)
        tn, fp, fn, tp = confusion_matrix(y_true, pred, labels=[0, 1]).ravel()
        fpr = fp / (fp + tn + 1e-9)
        specificity = tn / (tn + fp + 1e-9)
        oil_recall = tp / (tp + fn + 1e-9)
        bal_acc = balanced_accuracy_score(y_true, pred)
        rows.append(
            {
                "threshold": float(threshold),
                "false_positive_rate": float(fpr),
                "no_oil_specificity": float(specificity),
                "oil_recall": float(oil_recall),
                "balanced_accuracy": float(bal_acc),
                "accuracy": float(accuracy_score(y_true, pred)),
            }
        )
    candidates = [row for row in rows if row["false_positive_rate"] <= max_false_positive_rate]
    if not candidates:
        candidates = rows
    return max(
        candidates,
        key=lambda row: (
            row["balanced_accuracy"],
            row["no_oil_specificity"],
            row["oil_recall"],
            -row["threshold"],
        ),
    )


def calibration_group_splits(labels, groups):
    labels = np.asarray(labels)
    groups = np.asarray(groups)
    unique_groups = list(dict.fromkeys(groups.tolist()))
    for group in unique_groups:
        valid_idx = np.flatnonzero(groups == group)
        train_idx = np.flatnonzero(groups != group)
        if len(np.unique(labels[train_idx])) < 2:
            continue
        yield train_idx, valid_idx


def calibration_cv(
    df: pd.DataFrame,
    feature_columns: list[str],
    *,
    random_state: int,
    max_false_positive_rate: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    y = df["label"].astype(int).to_numpy()
    groups = df["file_id"].to_numpy()
    group_labels = df.groupby("file_id")["label"].first()
    min_groups_per_label = int(group_labels.value_counts().min())
    if min_groups_per_label < 2:
        raise ValueError("Need at least two calibration files per label for grouped calibration CV.")

    splits = list(calibration_group_splits(y, groups))
    n_splits = len(splits)
    if n_splits < 2:
        raise ValueError("Need at least two valid grouped calibration folds.")
    score_rows = []
    pred_frames = []
    model_thresholds: dict[str, float] = {}

    for model_name, model in make_models(random_state).items():
        oof_proba = np.full(len(df), np.nan)
        for train_idx, valid_idx in splits:
            fitted = clone(model)
            fitted.fit(df.iloc[train_idx][feature_columns], y[train_idx])
            oof_proba[valid_idx] = predict_proba_positive(fitted, df.iloc[valid_idx][feature_columns])

        threshold_info = choose_low_false_positive_threshold(
            y,
            oof_proba,
            max_false_positive_rate=max_false_positive_rate,
        )
        threshold = float(threshold_info["threshold"])
        pred = (oof_proba >= threshold).astype(int)
        tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
        model_thresholds[model_name] = threshold
        score_rows.append(
            {
                "model": model_name,
                "feature_count": len(feature_columns),
                "folds": n_splits,
                "threshold": threshold,
                "auc": safe_auc(y, oof_proba),
                "accuracy": accuracy_score(y, pred),
                "balanced_accuracy": balanced_accuracy_score(y, pred),
                "false_positive_rate": fp / (fp + tn + 1e-9),
                "no_oil_specificity": tn / (tn + fp + 1e-9),
                "oil_recall": tp / (tp + fn + 1e-9),
                "true_no_oil_pred_no_oil": int(tn),
                "true_no_oil_pred_oil": int(fp),
                "true_oil_pred_no_oil": int(fn),
                "true_oil_pred_oil": int(tp),
            }
        )
        pred_frame = df[["file_id", "file_name", "block_index", "label", "label_name", "meta_pos", "meta_dis_mm"]].copy()
        pred_frame["model"] = model_name
        pred_frame["prob_oil"] = oof_proba
        pred_frame["pred_label"] = pred
        pred_frame["correct"] = pred_frame["pred_label"] == pred_frame["label"].astype(int)
        pred_frames.append(pred_frame)

    scores = pd.DataFrame(score_rows).sort_values(
        ["balanced_accuracy", "no_oil_specificity", "oil_recall", "auc"],
        ascending=False,
    )
    return scores, pd.concat(pred_frames, ignore_index=True), model_thresholds


def train_final_model(df: pd.DataFrame, feature_columns: list[str], model_name: str, random_state: int):
    model = clone(make_models(random_state)[model_name])
    model.fit(df[feature_columns], df["label"].astype(int).to_numpy())
    return model


def activate_model(output_dir: Path, calibrated_model_path: Path) -> Path:
    active_path = output_dir / "oil_classifier_model.joblib"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = output_dir / f"oil_classifier_model_before_calibration_{stamp}.joblib"
    if active_path.exists():
        shutil.copy2(active_path, backup_path)
    shutil.copy2(calibrated_model_path, active_path)
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Train a real-RS485 calibrated oil classifier.")
    parser.add_argument("--calibration-root", type=Path, default=Path("shuju") / "实机标定数据")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--max-false-positive-rate", type=float, default=0.10)
    parser.add_argument("--activate", action="store_true", help="Replace outputs/oil_classifier_model.joblib with the calibrated model after backing it up.")
    args = parser.parse_args()

    calibration_root = args.calibration_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    records = load_calibration_records(calibration_root)
    if not records:
        raise SystemExit(f"No calibration DAT files found under {calibration_root}")
    df = build_feature_frame(records)
    label_counts = df["label_name"].value_counts().to_dict()
    if set(label_counts) != {NEGATIVE_LABEL_NAME, POSITIVE_LABEL_NAME}:
        raise SystemExit(f"Calibration data must contain both labels. Found: {label_counts}")

    feature_columns = numeric_feature_columns(df, include_metadata=False)
    scores, oof_predictions, model_thresholds = calibration_cv(
        df,
        feature_columns,
        random_state=args.random_state,
        max_false_positive_rate=args.max_false_positive_rate,
    )
    selected_row = scores.iloc[0]
    selected_model_name = str(selected_row["model"])
    selected_threshold = float(model_thresholds[selected_model_name])
    final_model = train_final_model(df, feature_columns, selected_model_name, args.random_state)

    df.to_csv(output_dir / "calibration_features.csv", index=False, encoding="utf-8-sig")
    scores.to_csv(output_dir / "calibrated_model_cv_scores.csv", index=False, encoding="utf-8-sig")
    oof_predictions.to_csv(output_dir / "calibration_oof_block_predictions.csv", index=False, encoding="utf-8-sig")

    calibrated_model_path = output_dir / "oil_classifier_model_calibrated.joblib"
    joblib.dump(
        {
            "model": final_model,
            "feature_columns": feature_columns,
            "threshold": selected_threshold,
            "selected_model_name": selected_model_name,
            "selected_feature_set": "real_rs485_calibration_stable_no_metadata",
            "positive_label": POSITIVE_LABEL_NAME,
            "negative_label": NEGATIVE_LABEL_NAME,
            "calibration_root": str(calibration_root),
            "max_false_positive_rate": args.max_false_positive_rate,
        },
        calibrated_model_path,
    )

    backup_path = None
    if args.activate:
        backup_path = activate_model(output_dir, calibrated_model_path)

    summary = {
        "calibration_root": str(calibration_root),
        "output_dir": str(output_dir),
        "calibration_files": int(df["file_id"].nunique()),
        "calibration_blocks": int(df.shape[0]),
        "label_counts": {str(k): int(v) for k, v in label_counts.items()},
        "selected_model_name": selected_model_name,
        "selected_threshold": selected_threshold,
        "selected_cv": selected_row.to_dict(),
        "model_path": str(calibrated_model_path),
        "activated_model_path": str(output_dir / "oil_classifier_model.joblib") if args.activate else None,
        "backup_model_path": str(backup_path) if backup_path else None,
        "notes": [
            "Only shuju/实机标定数据 is used for this calibration training run.",
            "Original 测试数据 files are not used.",
            "Validation is grouped by calibration DAT file so blocks from the same acquisition stay in one fold.",
            "Threshold selection constrains no-oil false positives before choosing the final active model.",
        ],
    }
    (output_dir / "calibrated_training_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
