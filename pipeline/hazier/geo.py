"""Geometry on a spherical Earth, shared by the model modules."""

import numpy as np

EARTH_RADIUS_M = 6_371_000.0  # mean radius; a spherical Earth is far more precise than the winds
EARTH_RADIUS_KM = EARTH_RADIUS_M / 1000.0


def great_circle_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Great-circle distance in km (haversine formula). Arguments broadcast; NaN gives NaN."""
    p1 = np.radians(np.asarray(lat1, dtype=float))
    p2 = np.radians(np.asarray(lat2, dtype=float))
    dl = np.radians(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float))
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.minimum(a, 1.0)))
