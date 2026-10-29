"""Offline geometric review only: no robot adapter, sockets or motion commands.

Run from site/: python -m tools.preview_smooth_transfer
The 5 mm corner shape is illustrative, not a TMflow trajectory simulation.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from tmchess.config import CONFIG


def preview(source, destination, *, approach=355.0, safe=420.0, radius=5.0):
    values = (*source, *destination, approach, safe, radius)
    if len(source) != 2 or len(destination) != 2 or not all(math.isfinite(x) for x in values):
        raise ValueError("Coordinates and settings must be finite XY values")
    if not 0 <= radius <= 5 or safe <= approach:
        raise ValueError("Require 0 <= radius <= 5 and safe > approach")
    distance = math.dist(source, destination)
    eligible = radius > 0 and min(distance, safe - approach) > 2 * radius
    return {
        "offline_only": True,
        "controller_path_verified": False,
        "source_xy": source,
        "destination_xy": destination,
        "horizontal_distance_mm": distance,
        "smoothing_candidate": eligible,
        "corner_radius_candidate_mm": radius if eligible else 0,
        "start_stop_z": approach,
        "upper_waypoint_z": safe,
        "end_stop_z": approach,
        "low_contact_segment_smoothed": False,
        "note": "Radius is a proposed controller setting, not proof of clearance or joint feasibility.",
    }


def main():
    motion = CONFIG.motion
    matrix = motion.board_xy_matrix()
    points = [xy for row in matrix for xy in row]
    plans = [preview(a, b, approach=motion.z_approach, safe=motion.z_safe)
             for i, a in enumerate(points) for j, b in enumerate(points) if i != j]
    target = Path(__file__).resolve().parents[2] / "outputs" / "smooth_transfer_preview"
    target.mkdir(parents=True, exist_ok=True)
    summary = {
        "mode": "OFFLINE_ONLY_NOT_CONNECTED_TO_ROBOT",
        "pairs_checked": len(plans),
        "geometric_candidates": sum(p["smoothing_candidate"] for p in plans),
        "minimum_xy_distance_mm": min(p["horizontal_distance_mm"] for p in plans),
        "speed_percent_current": motion.speed,
        "acceleration_time_ms_current": motion.acceleration,
        "radius_candidate_mm": 5,
        "approach_z": motion.z_approach,
        "safe_z": motion.z_safe,
        "contact_z": motion.z_pick,
        "hardware_verified": False,
        "examples": [plans[0], max(plans, key=lambda p: p["horizontal_distance_mm"])],
    }
    (target / "geometry.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
