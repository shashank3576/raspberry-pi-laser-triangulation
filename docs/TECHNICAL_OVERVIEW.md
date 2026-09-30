# Technical Overview

This note describes the implementation represented by the supplied source code and calibration model. It is intended to help a reviewer understand the measurement path without having to read both scripts first.

## 1. Measurement principle

The system projects a red laser onto a target and observes the spot with a camera. As the target height changes, the spot moves horizontally in the image. Calibration relates this horizontal displacement to known target heights.

The measurement script first captures a fresh 0 mm reference. For each object, it computes the mean horizontal spot position and subtracts the reference position:

`Δx = x_object − x_reference`

The calibration model maps `Δx` to estimated height in millimetres.

## 2. Image processing

The detector in `src/calibration.py` and `src/height_calculation.py` follows two stages:

1. **Coarse localisation:** crop the configured region of interest and select pixels where red intensity exceeds a minimum value and is sufficiently greater than green and blue intensity. Morphological opening reduces small isolated regions. The largest qualifying contour supplies a coarse spot location.
2. **Fine centroid:** examine a local window around the coarse location, smooth the red channel, estimate local background from a percentile, and retain the connected region above a relative intensity threshold. An intensity-weighted centroid provides the final sub-pixel spot position.

The saved configuration includes the region of interest, colour-difference thresholds, contour-area threshold, local window radius, smoothing level, background percentile, relative threshold, and saturation threshold. The calibration file also stores camera resolution, exposure, gain, and colour gains so measurement can reapply the settings.

## 3. Calibration model

The supplied model selects a rational function. With `u = Δx / 1000`, the stored model has the form:

`h(u) = (a u + b) / (c u + 1)`

The parameters and calibrated input range are stored in `data/calibration/calibration_model.json`. The model is fitted against the supplied known-height calibration points; do not reuse its coefficients for a different camera or physical arrangement without validation.

The saved model reports leave-one-out cross-validation RMSE of approximately 0.245 mm and a maximum absolute leave-one-out error of approximately 0.510 mm. These quantify performance under that cross-validation procedure on the supplied calibration dataset; they are not independent test-set results.

## 4. Repeatability and uncertainty

For each measurement, the script collects multiple valid detections and applies a median-absolute-deviation-based outlier filter to horizontal positions. It calculates the retained spot-position standard deviation, converts a repeatability term using the local model slope, and combines that term with the stored leave-one-out RMSE.

The resulting value is a model-based uncertainty estimate. It should not be described as a 95% confidence interval unless a separate statistical justification establishes that interpretation.

## 5. Data flow

| Stage | Main artifact |
|---|---|
| Individual calibration observations | `data/raw/all_measurements.csv` |
| Aggregated known-height measurements | `data/calibration/combined_calibration_data.csv` |
| Saved camera, detector, points, and model | `data/calibration/calibration_model.json` |
| Calibration comparison tables | `results/tables/` |
| Calibration plots | `results/figures/calibration/` |
| Object measurement output | `results/measurements/height_measurement_results.csv` |

## 6. Reproducibility checklist

Before collecting or comparing measurements:

- Keep camera, laser, target, and mounting geometry fixed.
- Use compatible Raspberry Pi OS camera support.
- Confirm the laser spot is within the configured region of interest.
- Avoid saturated laser spots and large changes in lighting.
- Record known reference heights and repeat measurements independently.
- Inspect the calibration plots and investigate points with unusually high variation.
- Back up supplied data before rerunning calibration, because the script regenerates output files.

## 7. Known validation boundary

The supplied evidence documents a calibration dataset and cross-validation results. A stronger performance claim would require independent known-height test objects, repeated sessions, and a documented comparison between predicted and reference heights. Measurements outside the calibrated input range are extrapolations and should be treated cautiously.
