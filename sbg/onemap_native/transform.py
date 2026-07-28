"""The correct OneMap 3D-Tiles -> EPSG:3414 coordinate transform.

THIS IS THE CRITICAL FIX. For most of this project's OneMap work, geometry was
placed by a hand-tuned "band-aid" (sign-flip the node translation `(-tx,+ty,-tz)`,
ignore the node rotation, apply a manual ENU basis at RTC_CENTER). That band-aid
was fundamentally WRONG -- it only happened to land correctly on the handful of
tiles whose node rotation was near the value it implicitly assumed. Measured
directly across a real domain: HALF of all leaf tiles placed their ground
anywhere from -130m to +266m, with large within-tile scatter -- invisible in a
top-down (XY) render, obvious the moment you look at Z.

The correct transform is the textbook 3D-Tiles one:

    p_ecef = RTC_CENTER + ZUP @ (R @ p_local + T)

where, per glTF node `p_local`:
  - R = the node's rotation quaternion, as a 3x3 matrix
  - T = the node's translation
  - ZUP = the glTF Y-up -> 3D-Tiles Z-up axis correction (+90 deg about X):
          (x, y, z) -> (x, -z, y)
  - RTC_CENTER = the b3dm feature table's RTC_CENTER (already ECEF)

then ECEF (EPSG:4978) -> WGS84 (EPSG:4979) -> SVY21 (EPSG:3414).

Verified across a good tile AND several tiles the band-aid placed hundreds of
meters wrong: every one lands every building base at exactly z=0 with zero
scatter (OneMap models all building bases at a single z=0 datum; real terrain
elevation is overlaid separately -- see sbg.onemap_native.terrain).
"""
import numpy as np
from pyproj import Transformer

_ecef_to_wgs84 = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)
_wgs84_to_svy21 = Transformer.from_crs("EPSG:4326", "EPSG:3414", always_xy=True)

# glTF Y-up -> 3D-Tiles Z-up (+90 deg about X): (x, y, z) -> (x, -z, y)
ZUP = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])


def quat_to_matrix(q):
    """glTF quaternion [x, y, z, w] -> 3x3 rotation matrix (normalized)."""
    x, y, z, w = q
    n = np.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def node_transform(node):
    """Extract (T, R) from a pygltflib node (identity defaults). R is a matrix."""
    T = np.array(node.translation) if (node is not None and node.translation) else np.zeros(3)
    R = quat_to_matrix(node.rotation) if (node is not None and node.rotation) else np.eye(3)
    return T, R


def local_to_svy21(local_pts, T, R, rtc_center):
    """Correct transform: RTC + ZUP @ (R @ p + T) -> ECEF -> EPSG:3414.

    local_pts: (N, 3) Draco-decoded glTF vertices in the node's local frame.
    Returns (N, 3) as (x, y, z) in EPSG:3414 meters (z = ellipsoidal height;
    building bases land at ~0 -- OneMap's single-datum convention).
    """
    ecef = rtc_center + (ZUP @ ((R @ local_pts.T).T + T).T).T
    lon, lat, h = _ecef_to_wgs84.transform(ecef[:, 0], ecef[:, 1], ecef[:, 2])
    x, y = _wgs84_to_svy21.transform(lon, lat)
    return np.column_stack([x, y, h])
