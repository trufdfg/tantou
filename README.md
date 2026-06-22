# 太赫兹探头油液识别

本项目用于太赫兹探头采集容器回波数据后的油液有无识别，包含离线训练、RS485 实时采集、GUI 实时判断和实机标定校准流程。

## 环境

使用本机 `research` conda 环境运行：

```powershell
conda activate research
```

## 主要入口

实时弹窗判断：

```powershell
python main.py
```

离线训练原始模型：

```powershell
python scripts/train_oil_classifier.py
```

根据 `shuju/实机标定数据` 训练实机校准候选模型：

```powershell
python scripts/train_calibrated_oil_classifier.py --calibration-root shuju\实机标定数据 --output-dir outputs
```

## 数据目录

- `shuju/训练数据`: 原始训练数据，按 `有油` / `无油` 和距离组织。
- `shuju/测试数据`: 原始保留测试数据，不参与训练。
- `shuju/实机标定数据`: GUI 标定按钮采集到的真实 RS485 数据。

## 输出目录

- `outputs/oil_classifier_model.joblib`: 当前 GUI 默认使用的模型。
- `outputs/oil_classifier_model_calibrated.joblib`: 实机标定训练得到的候选模型。
- `outputs/太赫兹探头油液识别训练报告.docx`: 训练总结报告。

实时串口日志目录已被 `.gitignore` 排除。
