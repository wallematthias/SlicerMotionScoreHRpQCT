# SlicerMotionScoreHRpQCT

SlicerMotionScoreHRpQCT is a 3D Slicer extension for fast, reviewer-driven motion grading of HR-pQCT scans using the MotionScore core pipeline. It supports one-click prediction, rapid manual grading, reviewer tracking, and export-ready grading tables for downstream analysis.

No known related patents.

License: MIT (see [LICENSE.txt](LICENSE.txt)).

![SlicerMotionScoreHRpQCT screenshot](resources/ScreenshotMotionScoreHRpQCT.png)

## Module Overview

### MotionScoreHRpQCT

This scripted module provides the full extension workflow:
- run `motionscore predict` from dataset root,
- review scans with quick grades and reviewer attribution,
- monitor agreement metrics,
- export consolidated final grade tables.

## Typical Tutorial Workflow

1. Set `Dataset Root`.
2. Click `Run Predict` (torch backend).
3. In `Review`, enter reviewer ID once.
4. For each scan, click quick grade (`Grade 1..5`) to save and advance.
5. Use `Back` to revisit and overwrite accidental grading.
6. Click `Export Final Grades` when complete.

Output path:
- `<dataset_root>/MotionScore`

## Core Pipeline Contract

This extension is GUI-only and delegates persistent data processing to `MotionScoreCNN`:
- `motionscore predict`
- `motionscore review-init`
- `motionscore review-apply`
- `motionscore review-clear`
- `motionscore export`

The extension does not maintain ad hoc sidecar state; review state is read from and written to derivatives managed by the core CLI.

## Privacy and Safety

- The extension does not upload user data by default.
- Network calls are only made when users explicitly trigger license setup/model download features.
- The extension does not include untrusted third-party binaries.
- Model weights are licensed for usage tracking; in the current deployment flow, licenses are automatically granted at signup.

## Publication

Walle, M., Eggemann, D., Atkins, P.R., Kendall, J.J., Stock, K., Müller, R. and Collins, C.J., 2023. Motion grading of high-resolution quantitative computed tomography supported by deep convolutional neural networks. *Bone*, 166, p.116607.  
https://doi.org/10.1016/j.bone.2022.116607

## Developer Install

1. Clone this repository.
2. Register module path:
   - `"/Applications/Slicer.app/Contents/MacOS/Slicer" --no-splash --no-main-window --python-script "<repo>/scripts/link_local_module.py"`
3. Restart Slicer and open module `MotionScoreHRpQCT`.
4. Install core package in Slicer Python:
   - `"/Applications/Slicer.app/Contents/bin/PythonSlicer" -m pip install -e "<path-to>/MotionScoreCNN"`

## CI/CD

- `CI` workflow validates:
  - module Python syntax,
  - extension metadata JSON,
  - packaged extension archive build.
- `Release` workflow publishes tagged extension artifacts (`v*` tags).
