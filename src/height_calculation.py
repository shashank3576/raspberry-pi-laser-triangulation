# ============================================================
# LASER TRIANGULATION - HEIGHT CALCULATION (v3)
# ============================================================
#
# Uses calibration_model.json produced by calibration.py.
# The calibration model is fitted using horizontal pixel shift:
#
#     ΔX = X_object - X_reference
#
# At startup the current 0-mm reference is measured first.
# This current reference is then used for every object, so a small
# common shift of the laser/camera setup is cancelled.
#
# Run: python3 height_calc.py
# ============================================================

import csv
import json
import os
import time
from pathlib import Path

import cv2
import numpy as np
from scipy.interpolate import PchipInterpolator

from picamera2 import Picamera2


# ============================================================
# CONFIGURATION
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_JSON_FILE = PROJECT_ROOT / "data" / "calibration" / "calibration_model.json"
RESULTS_DIR = PROJECT_ROOT / "results" / "measurements"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RESULT_FILE = RESULTS_DIR / "height_measurement_results.csv"

NUMBER_OF_OBJECTS = 5

MEASUREMENTS_PER_OBJECT = 20
REFERENCE_SHOTS = 30
DISCARD_FRAMES_AFTER_ENTER = 2
MAD_Z_THRESHOLD = 3.5

DETECTOR_VERSION = 3


# ============================================================
# LASER SPOT DETECTION
# IDENTICAL TO THE CALIBRATION CODE
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


# ============================================================
# OUTLIER REJECTION
# IDENTICAL TO THE CALIBRATION CODE
# ============================================================

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
# CALIBRATION MODEL
# IDENTICAL MODEL/NORMALIZATION TO CALIBRATION CODE
# ============================================================

X_CENTER = 0.0                            # calibration uses horizontal pixel shift
X_SCALE = 1000.0


def _u(x):
    return (np.asarray(x, dtype=float) - X_CENTER) / X_SCALE


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


# ============================================================
# LOAD CALIBRATION
# ============================================================

def load_calibration():

    if not os.path.exists(MODEL_JSON_FILE):
        raise FileNotFoundError(
            f"\n{MODEL_JSON_FILE} not found.\n"
            "Run calibration.py first and keep its output files in this folder."
        )

    with open(MODEL_JSON_FILE) as f:
        cal = json.load(f)

    if cal.get("detector_version") != DETECTOR_VERSION:
        raise RuntimeError(
            f"Detector version mismatch: calibration file is "
            f"v{cal.get('detector_version')}, this script is "
            f"v{DETECTOR_VERSION}. Re-run calibration.py from the same package."
        )

    if "reference" not in cal:
        raise RuntimeError(
            "calibration_model.json does not contain the calibration reference."
        )

    if "selected_model" not in cal:
        raise RuntimeError(
            "calibration_model.json does not contain the selected calibration model."
        )

    return cal


# ============================================================
# CAMERA
# Re-applies the locked calibration camera settings
# ============================================================

def init_camera(camera_settings):

    picam2 = Picamera2()

    config = picam2.create_still_configuration(
        main={"size": tuple(camera_settings["resolution"])}
    )

    picam2.configure(config)
    picam2.start()

    controls = {
        "AeEnable": False,
        "AwbEnable": False,
        "ExposureTime": int(camera_settings["exposure_time_us"]),
        "AnalogueGain": float(camera_settings["analogue_gain"]),
    }

    if camera_settings.get("colour_gains") is not None:
        controls["ColourGains"] = tuple(camera_settings["colour_gains"])

    picam2.set_controls(controls)

    time.sleep(2.0)

    for _ in range(5):
        picam2.capture_array()

    print(
        f"\nCamera started with calibration settings: "
        f"exposure = {controls['ExposureTime']} us, "
        f"gain = {controls['AnalogueGain']:.3f}"
    )

    return picam2


# ============================================================
# TAKE SHOTS
# ============================================================

def take_shots(picam2, cfg, count):

    for _ in range(DISCARD_FRAMES_AFTER_ENTER):
        picam2.capture_array()

    shots = []
    attempts = 0

    while len(shots) < count and attempts < count * 3:

        attempts += 1

        frame = picam2.capture_array()
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

        spot = detect_laser(frame_bgr, cfg)

        if spot is not None:
            shots.append(spot)

    return shots


# ============================================================
# RESULTS
# ============================================================

RESULT_FIELDS = [
    "object",
    "height_mm",
    "uncertainty_mm",
    "mean_x",
    "std_x",
    "mean_y",
    "std_y",
    "mean_x_shift",
    "mean_y_shift",
    "mean_beam_width",
    "valid_shots",
    "rejected_outliers",
    "extrapolated",
]


def save_results(rows):

    with open(RESULT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# MAIN
# ============================================================

def main():

    cal = load_calibration()

    cfg = cal["detector"]
    camera_settings = cal["camera"]

    model = HeightModel(
        cal["selected_model"]["name"],
        cal["selected_model"]["params"]
    )

    loo_rmse = float(cal["expected_error_loo_rmse_mm"])

    h_min, h_max = cal["calibrated_height_range_mm"]

    print("\n" + "=" * 72)
    print("LASER TRIANGULATION HEIGHT MEASUREMENT (v3)")
    print("=" * 72)
    print(f"Calibration file      : {MODEL_JSON_FILE}")
    print(f"Model                 : {model.name}")
    print(f"Calibrated range      : {h_min:.0f} - {h_max:.0f} mm")
    print(f"Calibration LOO RMSE  : {loo_rmse:.3f} mm")
    print("\nThe current 0-mm reference will be measured first.")

    picam2 = init_camera(camera_settings)
    results = []

    try:

        # ========================================================
        # CURRENT 0-MM REFERENCE
        # ========================================================

        input("\nPlace the 0 mm reference and press ENTER...")

        reference_shots = take_shots(
            picam2,
            cfg,
            REFERENCE_SHOTS
        )

        if not reference_shots:
            raise RuntimeError(
                "Laser not detected for the current 0-mm reference."
            )

        ref_x = np.array(
            [s["x"] for s in reference_shots],
            dtype=float
        )

        ref_y = np.array(
            [s["y"] for s in reference_shots],
            dtype=float
        )

        ref_width = np.array(
            [s["beam_width"] for s in reference_shots],
            dtype=float
        )

        ref_keep = inlier_mask(ref_x)

        reference_x = float(np.mean(ref_x[ref_keep]))
        reference_y = float(np.mean(ref_y[ref_keep]))
        reference_beam_width = float(np.mean(ref_width[ref_keep]))

        print("\n" + "-" * 72)
        print("CURRENT 0-MM REFERENCE")
        print("-" * 72)
        print(f"Average X          : {reference_x:.3f} px")
        print(f"Average Y          : {reference_y:.3f} px")
        print(f"Average beam width : {reference_beam_width:.2f} px")
        print(f"Shots used         : {ref_keep.sum()}/{len(reference_shots)}")

        # ========================================================
        # OBJECTS
        # ========================================================

        for object_number in range(1, NUMBER_OF_OBJECTS + 1):

            print("\n" + "=" * 72)

            input(
                f"Place object {object_number} of "
                f"{NUMBER_OF_OBJECTS} and press ENTER..."
            )

            shots = take_shots(
                picam2,
                cfg,
                MEASUREMENTS_PER_OBJECT
            )

            while not shots:

                print(
                    "Laser not detected - check the object is in place "
                    "and try again."
                )

                input("Press ENTER to retry...")

                shots = take_shots(
                    picam2,
                    cfg,
                    MEASUREMENTS_PER_OBJECT
                )

            xs = np.array([s["x"] for s in shots], dtype=float)
            ys = np.array([s["y"] for s in shots], dtype=float)
            widths = np.array([s["beam_width"] for s in shots], dtype=float)

            keep = inlier_mask(xs)

            fx = xs[keep]
            fy = ys[keep]
            fw = widths[keep]

            mean_x = float(np.mean(fx))
            mean_y = float(np.mean(fy))

            std_x = (
                float(np.std(fx, ddof=1))
                if len(fx) > 1 else 0.0
            )

            std_y = (
                float(np.std(fy, ddof=1))
                if len(fy) > 1 else 0.0
            )

            mean_beam_width = float(np.mean(fw))

            # Current-reference shifts
            mean_x_shift = mean_x - reference_x
            mean_y_shift = mean_y - reference_y

            # Height is predicted from ΔX, not absolute X.
            height, extrapolated = model.predict(mean_x_shift)

            slope = abs(model.slope_mm_per_px(mean_x_shift))

            repeatability_mm = (
                std_x / np.sqrt(len(fx))
            ) * slope

            uncertainty = float(
                np.sqrt(
                    repeatability_mm ** 2 +
                    loo_rmse ** 2
                )
            )

            print(f"\nOBJECT {object_number}")
            print("-" * 72)

            # Every individual reading
            for i, s in enumerate(shots):

                print(
                    f"Reading {i + 1:02d}: "
                    f"X = {s['x']:.3f} px   "
                    f"Y = {s['y']:.3f} px   "
                    f"ΔX = {s['x'] - reference_x:+.3f} px   "
                    f"ΔY = {s['y'] - reference_y:+.3f} px   "
                    f"beam width = {s['beam_width']:.1f} px"
                )

            print("-" * 72)
            print(f"Average X          : {mean_x:.3f} px")
            print(f"Average Y          : {mean_y:.3f} px")
            print(f"Average ΔX         : {mean_x_shift:+.3f} px")
            print(f"Average ΔY         : {mean_y_shift:+.3f} px")
            print(f"Average beam width : {mean_beam_width:.2f} px")
            print(f"Shots used         : {len(fx)}/{len(shots)}")

            if extrapolated:
                print(
                    f"[!] ΔX = {mean_x_shift:.1f} px is outside "
                    f"the calibrated range "
                    f"({model.x_min:.1f} - {model.x_max:.1f} px); "
                    "linear extrapolation is being used."
                )

            print(
                f"\nCALCULATED HEIGHT = "
                f"{height:.3f} mm   (+/- {uncertainty:.2f} mm)"
            )

            results.append({
                "object": object_number,
                "height_mm": round(height, 4),
                "uncertainty_mm": round(uncertainty, 4),
                "mean_x": round(mean_x, 4),
                "std_x": round(std_x, 4),
                "mean_y": round(mean_y, 4),
                "std_y": round(std_y, 4),
                "mean_x_shift": round(mean_x_shift, 4),
                "mean_y_shift": round(mean_y_shift, 4),
                "mean_beam_width": round(mean_beam_width, 4),
                "valid_shots": len(fx),
                "rejected_outliers": len(shots) - len(fx),
                "extrapolated": extrapolated,
            })

            save_results(results)

        # ========================================================
        # FINAL RESULTS
        # ========================================================

        print("\n" + "=" * 72)
        print("FINAL RESULTS")
        print("=" * 72)
        print(
            f"{'Object':>8} "
            f"{'Height (mm)':>14} "
            f"{'± (mm)':>10} "
            f"{'ΔX (px)':>12}"
        )
        print("-" * 72)

        for r in results:

            print(
                f"{r['object']:>8} "
                f"{r['height_mm']:>14.3f} "
                f"{r['uncertainty_mm']:>10.2f} "
                f"{r['mean_x_shift']:>12.3f}"
            )

        print("-" * 72)
        print(f"Results saved to {RESULT_FILE}")

    except KeyboardInterrupt:

        print("\nInterrupted by user.")

    finally:

        picam2.stop()
        print("\nCamera stopped.")


if __name__ == "__main__":
    main()