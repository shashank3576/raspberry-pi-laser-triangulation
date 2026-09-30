# ============================================================
# LASER TRIANGULATION - CALIBRATION (v3)
# ============================================================
#
# Calibrates on   : 0, 3, 6, ... 30 mm (3 mm steps)
#                   + anchor heights at 34, 40, 46 and 50 mm
#
# The second set of points extends the calibration through 50 mm.
# The random known-height objects add additional measured points to
# improve the fitted calibration curve.
#
# WHAT CHANGED vs the previous calibration.py
#   1. Camera exposure / gain / white balance are LOCKED and saved,
#      and height_calc.py re-applies exactly the same values.
#   2. New spot-centre detector: half-maximum, background-subtracted
#      centroid on the red channel. Much less sensitive to spot
#      brightness and to one-sided halos than the old two-stage one.
#   3. Several curve models are compared with leave-one-out
#      cross-validation (honest error estimate, not in-sample RMSE).
#   4. Everything height_calc.py needs is saved in
#      calibration_model.json (detector settings included), so the
#      two scripts can no longer drift apart.
#
# Run:   python3 calibration.py
# ============================================================

import csv
import datetime
import json
import time
from pathlib import Path

import cv2
import matplotlib
import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import least_squares

SHOW_PLOTS = False            # True only if you have a display attached
if not SHOW_PLOTS:
    matplotlib.use("Agg")
import matplotlib.pyplot as plt

from picamera2 import Picamera2


# ============================================================
# CONFIGURATION
# ============================================================

MAIN_HEIGHTS_MM = list(range(0, 31, 3))       # 0, 3, 6, ... 30
ANCHOR_HEIGHTS_MM = [34, 40, 46, 50]      # second calibration set
HEIGHTS_MM = MAIN_HEIGHTS_MM + ANCHOR_HEIGHTS_MM  # includes 30 mm again in the second stage
RANDOM_OBJECTS = 10                            # additional known-height objects
MEASUREMENTS_PER_HEIGHT = 30
DISCARD_FRAMES_AFTER_ENTER = 2                # throw away stale frames after placing the object

RESOLUTION = (2592, 1944)

# Leave both as None to lock whatever auto-exposure settles on.
# If the script warns about SATURATED spots, set a shorter exposure,
# e.g. EXPOSURE_TIME_US = 2000 (and lower the MIN_RED_* thresholds below if needed).
EXPOSURE_TIME_US = None
ANALOGUE_GAIN = None

MAD_Z_THRESHOLD = 3.5                         # outlier rejection inside each 30-shot block

MODEL_CHOICE = "auto"                         # "auto", "pchip", "rational", "poly3" or "poly4"

SUSPECT_LOO_MM = 0.25                         # flag calibration points that disagree with their neighbours


# ============================================================
# OUTPUT FILES
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
CALIBRATION_DATA_DIR = PROJECT_ROOT / "data" / "calibration"
RESULTS_TABLES_DIR = PROJECT_ROOT / "results" / "tables"
RESULTS_FIGURES_DIR = PROJECT_ROOT / "results" / "figures" / "calibration"

for _directory in (RAW_DATA_DIR, CALIBRATION_DATA_DIR, RESULTS_TABLES_DIR, RESULTS_FIGURES_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

RAW_CSV_FILE = RAW_DATA_DIR / "all_measurements.csv"
COMBINED_CSV_FILE = CALIBRATION_DATA_DIR / "combined_calibration_data.csv"
ANALYSIS_CSV_FILE = RESULTS_TABLES_DIR / "calibration_analysis.csv"
COEFFICIENTS_CSV_FILE = RESULTS_TABLES_DIR / "calibration_coefficients.csv"
MODEL_JSON_FILE = CALIBRATION_DATA_DIR / "calibration_model.json"


# ============================================================
# DETECTOR SETTINGS (saved into the JSON; height_calc.py uses the saved copy)
# ============================================================

DETECTOR_VERSION = 3

DETECTOR_CFG = {
    # region of interest (full-frame pixel coordinates)
    "roi_x_start": 900,
    "roi_x_end": 2400,
    "roi_y_start": 1250,
    "roi_y_end": 1550,

    # coarse "is there a red spot here?" test (same idea as before)
    "min_red_value": 80,
    "min_red_minus_green": 40,
    "min_red_minus_blue": 30,
    "min_contour_area": 15,

    # fine centre estimate
    "window_radius": 60,             # px around the coarse spot that are examined
    "smooth_sigma": 2.0,             # px, light blur before finding the peak
    "background_percentile": 10,     # local background level taken from the window
    "rel_threshold": 0.6,            # centroid uses pixels above 60 % of (peak - background)
    "min_peak_above_background": 30,
    "saturation_level": 250,         # red channel value treated as "clipped"
}


# ============================================================
# LASER SPOT DETECTION
# ============================================================

def detect_laser(frame_bgr, cfg):
    """Returns dict(x, y, peak_raw, saturated) in full-frame pixels, or None."""

    roi = frame_bgr[cfg["roi_y_start"]:cfg["roi_y_end"],
                    cfg["roi_x_start"]:cfg["roi_x_end"]]

    b, g, r = cv2.split(roi)
    r16 = r.astype(np.int16)
    g16 = g.astype(np.int16)
    b16 = b.astype(np.int16)

    # ---- stage 1: coarse localisation (red-dominant blob) ----
    coarse = (
        (r16 > cfg["min_red_value"]) &
        ((r16 - g16) > cfg["min_red_minus_green"]) &
        ((r16 - b16) > cfg["min_red_minus_blue"])
    ).astype(np.uint8) * 255

    coarse = cv2.morphologyEx(coarse, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

    contours, _ = cv2.findContours(coarse, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < cfg["min_contour_area"]:
        return None

    m = cv2.moments(largest)
    if m["m00"] == 0:
        return None
    coarse_x = m["m10"] / m["m00"]
    coarse_y = m["m01"] / m["m00"]

    # ---- stage 2: fine centre from the RED channel only ----
    height, width = r.shape
    rad = cfg["window_radius"]
    x0 = max(int(round(coarse_x)) - rad, 0)
    x1 = min(int(round(coarse_x)) + rad + 1, width)
    y0 = max(int(round(coarse_y)) - rad, 0)
    y1 = min(int(round(coarse_y)) + rad + 1, height)

    window = r[y0:y1, x0:x1].astype(np.float32)
    smooth = cv2.GaussianBlur(window, (0, 0), cfg["smooth_sigma"])

    background = float(np.percentile(window, cfg["background_percentile"]))
    peak_pos = np.unravel_index(np.argmax(smooth), smooth.shape)
    peak = float(smooth[peak_pos]) - background

    if peak < cfg["min_peak_above_background"]:
        return None

    threshold = cfg["rel_threshold"] * peak
    above = ((smooth - background) >= threshold).astype(np.uint8)

    # keep only the connected blob that contains the peak
    _, labels = cv2.connectedComponents(above, connectivity=8)
    blob = labels == labels[peak_pos]

    weights = np.where(blob, np.clip(smooth - background - threshold, 0, None), 0.0)
    total = float(weights.sum())
    if total <= 0:
        return None

    yy, xx = np.mgrid[y0:y1, x0:x1]
    cx = float((xx * weights).sum() / total) + cfg["roi_x_start"]
    cy = float((yy * weights).sum() / total) + cfg["roi_y_start"]

    peak_raw = float(window.max())

    blob_y, blob_x = np.where(blob)
    beam_width = int(blob_x.max() - blob_x.min() + 1) if len(blob_x) else 0

    return {
        "x": cx,
        "y": cy,
        "peak_raw": peak_raw,
        "saturated": peak_raw >= cfg["saturation_level"],
        "beam_width": beam_width,
    }


def inlier_mask(values, z_threshold=MAD_Z_THRESHOLD):
    """MAD-based outlier rejection. Returns a boolean keep-mask."""

    v = np.asarray(values, dtype=float)
    if len(v) < 5:
        return np.ones(len(v), dtype=bool)

    deviation = np.abs(v - np.median(v))
    mad = np.median(deviation)
    if mad == 0:
        return np.ones(len(v), dtype=bool)

    keep = (0.6745 * deviation / mad) < z_threshold
    if keep.sum() < 3:
        return np.ones(len(v), dtype=bool)
    return keep


# ============================================================
# CAMERA (exposure / gain / white balance are LOCKED)
# ============================================================

def init_camera():

    picam2 = Picamera2()
    config = picam2.create_still_configuration(main={"size": RESOLUTION})
    picam2.configure(config)
    picam2.start()

    print("\nCamera started - letting auto-exposure settle once...")
    time.sleep(2.0)

    metadata = picam2.capture_metadata()

    exposure = EXPOSURE_TIME_US if EXPOSURE_TIME_US is not None else int(metadata["ExposureTime"])
    gain = ANALOGUE_GAIN if ANALOGUE_GAIN is not None else float(metadata["AnalogueGain"])
    colour_gains = metadata.get("ColourGains")

    controls = {
        "AeEnable": False,
        "AwbEnable": False,
        "ExposureTime": int(exposure),
        "AnalogueGain": float(gain),
    }
    if colour_gains is not None:
        controls["ColourGains"] = tuple(float(c) for c in colour_gains)

    picam2.set_controls(controls)
    time.sleep(1.0)
    for _ in range(5):
        picam2.capture_array()

    settings = {
        "resolution": list(RESOLUTION),
        "exposure_time_us": int(exposure),
        "analogue_gain": float(gain),
        "colour_gains": [float(c) for c in colour_gains] if colour_gains is not None else None,
    }

    print(f"Locked camera: exposure = {exposure} us, gain = {gain:.3f}, "
          f"colour gains = {settings['colour_gains']}")

    return picam2, settings


def grab_spot(picam2):

    frame = picam2.capture_array()
    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    return detect_laser(frame_bgr, DETECTOR_CFG)


# ============================================================
# CALIBRATION MODELS
# ============================================================

X_CENTER = 0.0                            # calibration uses horizontal pixel shift
X_SCALE = 1000.0


def _u(x):
    return (np.asarray(x, dtype=float) - X_CENTER) / X_SCALE


CANDIDATE_MODELS = ["pchip", "rational", "poly3", "poly4"]

MIN_POINTS = {"pchip": 4, "rational": 6, "poly3": 6, "poly4": 7}


def fit_model(name, x, h):
    """Fit one model; returns a JSON-friendly parameter dict."""

    x = np.asarray(x, dtype=float)
    h = np.asarray(h, dtype=float)
    order = np.argsort(x)
    x, h = x[order], h[order]

    params = {"x_min": float(x[0]), "x_max": float(x[-1])}
    u = _u(x)

    if name == "pchip":
        params["x"] = x.tolist()
        params["h"] = h.tolist()

    elif name in ("poly3", "poly4"):
        params["coefficients"] = np.polyfit(u, h, int(name[-1])).tolist()

    elif name == "rational":
        # physical model of a laser triangulation rig: h = (a*u + b) / (c*u + 1)
        # (linear least squares gives the start point, then refine on the true residuals)
        A = np.column_stack([u, np.ones_like(u), -u * h])
        start = np.linalg.lstsq(A, h, rcond=None)[0]
        solution = least_squares(
            lambda p: (p[0] * u + p[1]) / (p[2] * u + 1.0) - h, start
        )
        a, b, c = solution.x
        denominator = c * u + 1.0
        if np.any(denominator < 0.2):
            raise ValueError("rational fit unstable")
        params.update({"a": float(a), "b": float(b), "c": float(c)})

    else:
        raise ValueError(f"unknown model {name}")

    return params


class HeightModel:
    """Pixel-X -> height (mm). Identical class is used in height_calc.py."""

    def __init__(self, name, params):
        self.name = name
        self.params = params
        self.x_min = float(params["x_min"])
        self.x_max = float(params["x_max"])
        if name == "pchip":
            self._pchip = PchipInterpolator(
                np.asarray(params["x"], float), np.asarray(params["h"], float),
                extrapolate=False
            )

    def inside(self, x):
        """Height for x inside the calibrated pixel range (array in, array out)."""
        x = np.asarray(x, dtype=float)
        if self.name == "pchip":
            return self._pchip(x)
        u = _u(x)
        if self.name in ("poly3", "poly4"):
            return np.polyval(self.params["coefficients"], u)
        p = self.params
        return (p["a"] * u + p["b"]) / (p["c"] * u + 1.0)

    def predict(self, x):
        """Returns (height_mm, extrapolated). Outside the calibrated range the
        result continues linearly with the slope at the edge of the range."""
        x = float(x)
        if x < self.x_min:
            edge = self.x_min
            slope = float(self.inside(edge + 1.0) - self.inside(edge))
            return float(self.inside(edge)) + slope * (x - edge), True
        if x > self.x_max:
            edge = self.x_max
            slope = float(self.inside(edge) - self.inside(edge - 1.0))
            return float(self.inside(edge)) + slope * (x - edge), True
        return float(self.inside(x)), False

    def slope_mm_per_px(self, x):
        return (self.predict(x + 0.5)[0] - self.predict(x - 0.5)[0])


def leave_one_out_errors(name, x, h):
    """Predict every INTERIOR calibration point from all the others.
    Returns array of errors (mm) for points 1..n-2, or None if not possible."""

    n = len(x)
    if n < MIN_POINTS[name] + 1:
        return None

    errors = []
    for i in range(1, n - 1):
        keep = np.arange(n) != i
        try:
            model = HeightModel(name, fit_model(name, x[keep], h[keep]))
            errors.append(float(model.inside(x[i])) - h[i])
        except Exception:
            return None
    return np.array(errors)


def evaluate_models(x, h):

    results = []

    for name in CANDIDATE_MODELS:

        if len(x) < MIN_POINTS[name]:
            continue

        try:
            params = fit_model(name, x, h)
            model = HeightModel(name, params)
        except Exception:
            continue

        loo = leave_one_out_errors(name, x, h)
        if loo is None:
            continue

        in_sample = model.inside(x) - h

        results.append({
            "name": name,
            "params": params,
            "model": model,
            "in_sample_errors": in_sample,
            "in_sample_rmse": float(np.sqrt(np.mean(in_sample ** 2))),
            "loo_errors": loo,
            "loo_rmse": float(np.sqrt(np.mean(loo ** 2))),
            "loo_max": float(np.max(np.abs(loo))),
        })

    if not results:
        raise RuntimeError("No calibration model could be fitted - need at least 6 points.")

    if MODEL_CHOICE == "auto":
        selected = min(results, key=lambda r: r["loo_rmse"])
    else:
        matches = [r for r in results if r["name"] == MODEL_CHOICE]
        if not matches:
            raise RuntimeError(f"MODEL_CHOICE '{MODEL_CHOICE}' could not be fitted.")
        selected = matches[0]

    return results, selected


# ============================================================
# SAVING
# ============================================================

def save_raw(raw_rows):

    with open(RAW_CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "height_mm", "measurement_number", "pixel_x", "pixel_y",
            "pixel_x_shift", "pixel_y_shift", "beam_width",
            "peak_red", "saturated", "used_in_average"
        ])
        writer.writeheader()
        writer.writerows(raw_rows)


def save_combined(rows):

    with open(COMBINED_CSV_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["height_mm", "mean_x", "mean_y", "mean_x_shift", "mean_y_shift",
                         "median_x", "median_y", "std_x", "std_y", "mean_beam_width",
                         "valid_measurements", "saturated_shots"])
        for r in rows:
            writer.writerow([r["height_mm"], r["mean_x"], r["mean_y"], r["mean_x_shift"],
                             r["mean_y_shift"], r["median_x"], r["median_y"], r["std_x"],
                             r["std_y"], r["mean_beam_width"], r["valid"], r["saturated_shots"]])


def save_analysis(rows, results, selected):

    heights = [r["height_mm"] for r in rows]

    with open(ANALYSIS_CSV_FILE, "w", newline="") as f:
        writer = csv.writer(f)

        header = ["height_mm", "mean_x", "mean_x_shift", "mean_y_shift",
                  "mean_beam_width", "std_x", "valid_measurements"]
        for res in results:
            header.append(f"{res['name']}_leave_one_out_error_mm")
        header.append(f"selected_({selected['name']})_in_sample_error_mm")
        writer.writerow(header)

        for i, r in enumerate(rows):
            line = [heights[i], r["mean_x"], r["mean_x_shift"], r["mean_y_shift"],
                    r["mean_beam_width"], r["std_x"], r["valid"]]
            for res in results:
                if 1 <= i <= len(rows) - 2:
                    line.append(res["loo_errors"][i - 1])
                else:
                    line.append("")
            line.append(selected["in_sample_errors"][i])
            writer.writerow(line)


def save_coefficients(results, selected):

    with open(COEFFICIENTS_CSV_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["model", "selected", "in_sample_rmse_mm",
                         "leave_one_out_rmse_mm", "leave_one_out_max_mm", "parameters"])
        for res in results:
            writer.writerow([
                res["name"], res["name"] == selected["name"],
                res["in_sample_rmse"], res["loo_rmse"], res["loo_max"],
                json.dumps({k: v for k, v in res["params"].items() if k not in ("x", "h")})
            ])


def save_model_json(rows, selected, camera_settings, reference_x, reference_y):

    x = [r["mean_x"] for r in rows]
    x_shift = [r["mean_x_shift"] for r in rows]
    h = [r["height_mm"] for r in rows]

    payload = {
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "detector_version": DETECTOR_VERSION,
        "detector": DETECTOR_CFG,
        "camera": camera_settings,
        "reference": {
            "height_mm": 0.0,
            "x": float(reference_x),
            "y": float(reference_y),
        },
        "points": [
            {"height_mm": r["height_mm"], "mean_x": r["mean_x"],
             "mean_y": r["mean_y"], "mean_x_shift": r["mean_x_shift"],
             "mean_y_shift": r["mean_y_shift"], "mean_beam_width": r["mean_beam_width"],
             "std_x": r["std_x"], "valid": r["valid"]}
            for r in rows
        ],
        "calibrated_height_range_mm": [min(h), max(h)],
        "calibrated_x_range_px": [min(x), max(x)],
        "calibrated_x_shift_range_px": [min(x_shift), max(x_shift)],
        "selected_model": {"name": selected["name"], "params": selected["params"]},
        "expected_error_loo_rmse_mm": selected["loo_rmse"],
        "expected_error_loo_max_mm": selected["loo_max"],
    }

    with open(MODEL_JSON_FILE, "w") as f:
        json.dump(payload, f, indent=2)

# ============================================================
# PLOTS
# ============================================================

def make_plots(rows, results, selected):

    heights = np.array([r["height_mm"] for r in rows], dtype=float)
    mean_x = np.array([r["mean_x"] for r in rows], dtype=float)
    mean_x_shift = np.array([r["mean_x_shift"] for r in rows], dtype=float)
    std_x = np.array([r["std_x"] for r in rows], dtype=float)
    std_y = np.array([r["std_y"] for r in rows], dtype=float)

    # 1. fit comparison - calibration is fitted using horizontal pixel shift
    x_dense = np.linspace(mean_x_shift.min(), mean_x_shift.max(), 600)
    plt.figure(figsize=(10, 7))
    plt.scatter(mean_x_shift, heights, color="black", zorder=5, label="Calibration points")
    for res in results:
        plt.plot(x_dense, res["model"].inside(x_dense),
                 label=f"{res['name']}  (LOO RMSE {res['loo_rmse']:.3f} mm)")
    plt.xlabel("Horizontal pixel shift from 0 mm reference (pixels)"); plt.ylabel("Height (mm)")
    plt.title("Calibration Fit Comparison - Height vs Horizontal Pixel Shift")
    plt.grid(True); plt.legend(); plt.tight_layout()
    plt.savefig(RESULTS_FIGURES_DIR / "calibration_fit_comparison.png", dpi=200)

    # 2. absolute horizontal pixel position diagnostic graph
    order = np.argsort(heights)
    plt.figure(figsize=(10, 7))
    plt.scatter(mean_x, heights, color="black", zorder=5, label="Calibration points")
    plt.xlabel("Horizontal pixel position"); plt.ylabel("Height (mm)")
    plt.title("Height vs Horizontal Pixel Position")
    plt.grid(True); plt.legend(); plt.tight_layout()
    plt.savefig(RESULTS_FIGURES_DIR / "height_vs_horizontal_pixels.png", dpi=200)

    # 3. leave-one-out error comparison
    plt.figure(figsize=(10, 7))
    for res in results:
        plt.plot(heights[1:-1], res["loo_errors"], marker="o", label=res["name"])
    plt.axhline(0, linestyle="--")
    plt.xlabel("Actual height (mm)"); plt.ylabel("Leave-one-out error (mm)")
    plt.title("Calibration Error Comparison (each point predicted from the others)")
    plt.grid(True); plt.legend(); plt.tight_layout()
    plt.savefig(RESULTS_FIGURES_DIR / "calibration_error_comparison.png", dpi=200)

    # 4. pixel standard deviation
    plt.figure(figsize=(10, 7))
    plt.plot(heights, std_x, marker="o", label="X standard deviation")
    plt.plot(heights, std_y, marker="s", label="Y standard deviation")
    plt.xlabel("Height (mm)"); plt.ylabel("Pixel standard deviation")
    plt.title("Pixel Position Standard Deviation"); plt.grid(True); plt.legend(); plt.tight_layout()
    plt.savefig(RESULTS_FIGURES_DIR / "pixel_position_standard_deviation.png", dpi=200)

    # 5. selected model residuals
    plt.figure(figsize=(10, 7))
    plt.scatter(heights, selected["in_sample_errors"], label="In-sample residual")
    plt.scatter(heights[1:-1], selected["loo_errors"], marker="x", label="Leave-one-out error")
    plt.axhline(0, linestyle="--")
    plt.xlabel("Actual height (mm)"); plt.ylabel("Error (mm)")
    plt.title(f"Selected model: {selected['name']}")
    plt.grid(True); plt.legend(); plt.tight_layout()
    plt.savefig(RESULTS_FIGURES_DIR / "calibration_fit_residuals.png", dpi=200)

    if SHOW_PLOTS:
        plt.show()
    plt.close("all")

# ============================================================
# ANALYSIS
# ============================================================

def analyse(rows, camera_settings, reference_x, reference_y):

    rows = sorted(rows, key=lambda r: r["height_mm"])
    heights = np.array([r["height_mm"] for r in rows], dtype=float)
    x = np.array([r["mean_x_shift"] for r in rows], dtype=float)

    if np.any(np.diff(x) <= 0):
        bad = [f"{heights[i]:.0f}->{heights[i + 1]:.0f} mm" for i in np.where(np.diff(x) <= 0)[0]]
        raise RuntimeError(
            "Horizontal pixel shift does not increase with height between: " + ", ".join(bad) +
            ". Check those placements and re-run."
        )

    save_combined(rows)

    results, selected = evaluate_models(x, heights)

    print("\n" + "=" * 72)
    print("CALIBRATION MODELS  (error = predicted - actual, in mm)")
    print("=" * 72)
    print(f"{'model':<10}{'in-sample RMSE':>16}{'leave-one-out RMSE':>22}{'LOO max':>10}")
    for res in results:
        tag = "   <-- selected" if res["name"] == selected["name"] else ""
        print(f"{res['name']:<10}{res['in_sample_rmse']:>16.3f}{res['loo_rmse']:>22.3f}"
              f"{res['loo_max']:>10.3f}{tag}")
    print("\nIn-sample RMSE can look perfect for pchip (it passes through every point).")
    print("Leave-one-out RMSE is the realistic accuracy estimate.")

    suspects = []
    for i in range(1, len(rows) - 1):
        err = selected["loo_errors"][i - 1]
        if abs(err) > SUSPECT_LOO_MM:
            suspects.append(f"{heights[i]:.0f} mm ({err:+.2f} mm)")
    if suspects:
        print("\nPoints that disagree with their neighbours (worth re-placing / re-measuring):")
        print("   " + ", ".join(suspects))

    slope_lo = (x[1] - x[0]) / (heights[1] - heights[0])
    slope_hi = (x[-1] - x[-2]) / (heights[-1] - heights[-2])
    print(f"\nSensitivity: {slope_lo:.1f} px/mm at the low end, {slope_hi:.1f} px/mm at the high end")
    print(f"Reference: X = {reference_x:.3f} px, Y = {reference_y:.3f} px")
    print(f"Calibrated range: {heights.min():.0f} - {heights.max():.0f} mm "
          f"(horizontal pixel shift {x.min():.1f} - {x.max():.1f})")

    print("\n" + "=" * 120)
    print("CALIBRATION READING SUMMARY")
    print("=" * 120)
    print(f"{'Height':>10} {'Avg X':>12} {'Avg Y':>12} {'Avg ΔX':>12} {'Avg ΔY':>12} {'Beam width':>14} {'Valid':>8}")
    print("-" * 120)
    for r in rows:
        print(f"{r['height_mm']:>10.3f} {r['mean_x']:>12.3f} {r['mean_y']:>12.3f} "
              f"{r['mean_x_shift']:>12.3f} {r['mean_y_shift']:>12.3f} "
              f"{r['mean_beam_width']:>14.2f} {r['valid']:>8d}")
    print("=" * 120)

    saturated_total = sum(r["saturated_shots"] for r in rows)
    if saturated_total:
        print(f"\n[!] {saturated_total} shots had a SATURATED red channel (>= "
              f"{DETECTOR_CFG['saturation_level']}). Saturation makes the centre less repeatable.")
        print("    Lower EXPOSURE_TIME_US (or reduce laser power) and re-run.")

    save_analysis(rows, results, selected)
    save_coefficients(results, selected)
    save_model_json(rows, selected, camera_settings, reference_x, reference_y)
    make_plots(rows, results, selected)

    print("\n" + "=" * 72)
    print("CALIBRATION COMPLETE")
    print("=" * 72)
    print(f"Selected model : {selected['name']}")
    print(f"Expected error : about {selected['loo_rmse']:.2f} mm RMS "
          f"(worst leave-one-out case {selected['loo_max']:.2f} mm)")
    print(f"\nFiles written: {RAW_CSV_FILE}, {COMBINED_CSV_FILE}, {ANALYSIS_CSV_FILE},")
    print(f"               {COEFFICIENTS_CSV_FILE}, {MODEL_JSON_FILE}")
    print("Plots        : calibration_fit_comparison.png, height_vs_horizontal_pixels.png,")
    print("               calibration_error_comparison.png, pixel_position_standard_deviation.png,")
    print("               calibration_fit_residuals.png")
    print(f"\nKeep {MODEL_JSON_FILE} next to height_calc.py.")

# ============================================================
# MAIN
# ============================================================

def main():

    print("\n" + "=" * 72)
    print("LASER TRIANGULATION CALIBRATION (v3)")
    print("=" * 72)
    print(f"Heights           : {HEIGHTS_MM} mm")
    print(f"Shots per height  : {MEASUREMENTS_PER_HEIGHT}")
    print(f"Random objects    : {RANDOM_OBJECTS}")
    print("Do not touch the camera, laser or exposure settings between heights.")
    print("Place every reference in the SAME position on the stage - only the height changes.")

    picam2, camera_settings = init_camera()

    raw_rows = []
    summary_rows = []

    try:

        for height in HEIGHTS_MM:

            while True:

                input(f"\nPlace the {height} mm reference and press ENTER...")

                for _ in range(DISCARD_FRAMES_AFTER_ENTER):
                    picam2.capture_array()

                shots = []
                attempts = 0

                while len(shots) < MEASUREMENTS_PER_HEIGHT and attempts < MEASUREMENTS_PER_HEIGHT * 3:
                    attempts += 1
                    spot = grab_spot(picam2)
                    if spot is not None:
                        shots.append(spot)

                if len(shots) >= 3:
                    break

                print("Laser not detected reliably - check the spot is inside the ROI and try again.")

            xs = np.array([s["x"] for s in shots])
            ys = np.array([s["y"] for s in shots])
            widths = np.array([s["beam_width"] for s in shots], dtype=float)
            keep = inlier_mask(xs)

            for k, s in enumerate(shots):
                raw_rows.append({
                    "height_mm": height,
                    "measurement_number": k + 1,
                    "pixel_x": s["x"],
                    "pixel_y": s["y"],
                    "pixel_x_shift": "",
                    "pixel_y_shift": "",
                    "beam_width": s["beam_width"],
                    "peak_red": s["peak_raw"],
                    "saturated": int(s["saturated"]),
                    "used_in_average": int(keep[k]),
                })

            fx, fy = xs[keep], ys[keep]
            fw = widths[keep]
            saturated_shots = int(sum(s["saturated"] for s in shots))

            summary_rows.append({
                "height_mm": float(height),
                "mean_x": float(np.mean(fx)),
                "mean_y": float(np.mean(fy)),
                "mean_x_shift": 0.0,
                "mean_y_shift": 0.0,
                "median_x": float(np.median(fx)),
                "median_y": float(np.median(fy)),
                "std_x": float(np.std(fx, ddof=1)) if len(fx) > 1 else 0.0,
                "std_y": float(np.std(fy, ddof=1)) if len(fy) > 1 else 0.0,
                "mean_beam_width": float(np.mean(fw)),
                "valid": int(len(fx)),
                "saturated_shots": saturated_shots,
            })

            last = summary_rows[-1]
            print(f"  {height:>3} mm: X = {last['mean_x']:.3f} px  Y = {last['mean_y']:.3f} px  "
                  f"beam width = {last['mean_beam_width']:.1f} px  (std X {last['std_x']:.3f}), "
                  f"{last['valid']}/{len(shots)} shots used, "
                  f"peak red {max(s['peak_raw'] for s in shots):.0f}"
                  + ("   [!] SATURATED" if saturated_shots else ""))

            save_raw(raw_rows)

        # The 0 mm measurement is the reference for all pixel shifts.
        reference = next(r for r in summary_rows if r["height_mm"] == 0)
        reference_x = reference["mean_x"]
        reference_y = reference["mean_y"]

        for r in summary_rows:
            r["mean_x_shift"] = r["mean_x"] - reference_x
            r["mean_y_shift"] = r["mean_y"] - reference_y

        for raw in raw_rows:
            raw["pixel_x_shift"] = raw["pixel_x"] - reference_x
            raw["pixel_y_shift"] = raw["pixel_y"] - reference_y

        # --------------------------------------------------------

        analyse(summary_rows, camera_settings, reference_x, reference_y)

    except KeyboardInterrupt:
        print("\nCalibration interrupted by user - nothing was finalised.")

    finally:
        picam2.stop()
        print("\nCamera stopped.")

if __name__ == "__main__":
    main()