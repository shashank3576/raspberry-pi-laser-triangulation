# Raspberry Pi Laser Triangulation Height Measurement

A camera-based laser triangulation project for estimating object height from the horizontal displacement of a red laser spot in camera images. The project uses a Raspberry Pi camera, a red-spot detector, a measured calibration dataset, and a fitted height-versus-pixel-shift model.

![Calibration fit comparison](results/figures/calibration/calibration_fit_comparison.jpg)

## Project at a glance

- **Platform:** Raspberry Pi 4 Model B
- **Camera:** Raspberry Pi Camera Module Rev 1.3 (OV5647), configured at 2592 × 1944 pixels
- **Measurement principle:** track the laser spot's horizontal pixel coordinate and convert its shift relative to a 0 mm reference into height
- **Spot detector:** red-dominant region detection followed by a local, background-subtracted centroid estimate
- **Calibration model in the supplied JSON:** rational model
- **Calibration range in the supplied model:** 0–50 mm

## Calibration snapshot

The supplied calibration model contains the following reference heights: 0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 34, 40, 46, and 50 mm.

| Metric | Value in supplied model |
|---|---:|
| Leave-one-out cross-validation RMSE | 0.245 mm |
| Maximum absolute leave-one-out error | 0.510 mm |
| Calibrated height interval | 0–50 mm |
| Selected model | Rational |

**Interpretation:** these error values are leave-one-out cross-validation results on the calibration points. They are not a guarantee of real-world accuracy on new objects or under changed lighting, focus, geometry, or camera settings. The 40 mm calibration point also has a notably larger horizontal pixel-position standard deviation than most other calibration points, so repeatability should be checked in further experiments.

## Repository structure

```text
raspberry-pi-laser-triangulation/
├── README.md
├── .gitignore
├── requirements.txt
├── src/
│   ├── calibration.py
│   └── height_calculation.py
├── data/
│   ├── raw/
│   │   └── all_measurements.csv
│   └── calibration/
│       ├── calibration_model.json
│       └── combined_calibration_data.csv
├── results/
│   ├── figures/calibration/
│   ├── measurements/height_measurement_results.csv
│   └── tables/
│       ├── calibration_analysis.csv
│       └── calibration_coefficients.csv
├── images/laser_spot/
└── docs/
    ├── report/Laser_Triangulation_Project_Report.pdf
    └── presentation/Laser_Triangulation_Project_Presentation.pptx
```

## Calibration figures

### Height versus horizontal pixel position
![Height versus pixel position](results/figures/calibration/height_vs_pixel_position.jpg)

### Leave-one-out error comparison
![Leave-one-out error comparison](results/figures/calibration/leave_one_out_error_comparison.jpg)

### Pixel-position standard deviation
![Pixel-position standard deviation](results/figures/calibration/pixel_position_standard_deviation.jpg)

## Hardware and software

The measurement scripts use `Picamera2` to capture images. The camera and laser geometry must remain fixed between calibration and measurement; changing the mount, focus, camera controls, or laser position can invalidate the calibration.

On Raspberry Pi OS, install the camera and image-processing packages using the OS package manager. A typical starting point is:

```bash
sudo apt update
sudo apt install python3-picamera2 python3-opencv python3-numpy python3-scipy python3-matplotlib
```

If your Raspberry Pi OS release uses different package names or you use a virtual environment, follow the installation guidance for that OS release. `Picamera2` is normally installed through Raspberry Pi OS packages rather than as an ordinary cross-platform pip dependency.

## Running the scripts

Run commands from the repository root so the paths and outputs remain easy to find.

### 1. Re-run calibration

```bash
python3 src/calibration.py
```

Place the known-height calibration targets as prompted by the script. The script captures fresh measurements and writes calibration data to `data/` and figures/tables to `results/`. **This operation regenerates files and can overwrite the included calibration outputs**, so back them up first if you need to preserve the supplied results.

### 2. Measure object heights

```bash
python3 src/height_calculation.py
```

The script loads `data/calibration/calibration_model.json`, measures the current reference, and saves measurement results to `results/measurements/height_measurement_results.csv`.

These commands require the project hardware, a compatible Raspberry Pi OS installation, and an attached/configured camera. They are not expected to run on a typical desktop computer without the camera stack.

## Data files

- `data/raw/all_measurements.csv` — individual laser-spot observations collected during calibration.
- `data/calibration/combined_calibration_data.csv` — aggregated calibration points used to fit the model.
- `data/calibration/calibration_model.json` — detector/camera settings, calibration points, selected model parameters, and cross-validation summary used by the measurement script.
- `results/tables/calibration_analysis.csv` — per-height analysis and leave-one-out errors for candidate models.
- `results/tables/calibration_coefficients.csv` — candidate model summaries and fitted parameters.
- `results/measurements/height_measurement_results.csv` — supplied height-measurement output.

## Limitations and reproducibility notes

- Calibration is valid for the physical setup and camera settings represented by the saved model; re-check it after any mechanical or optical changes.
- The reported leave-one-out error is calculated from the calibration dataset and should not be presented as a universal accuracy specification.
- The supplied scripts have been organized to use project-relative paths. The detection, fitting, and measurement algorithms have otherwise been retained.
- The included report and presentation provide additional project context and are preserved as supporting documents.
