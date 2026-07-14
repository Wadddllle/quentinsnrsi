"""Minimal B3DM header parser: batch-table-only, no mesh/GLB extraction.

B3DM header (28 bytes, little-endian): magic, version, byteLength,
featureTableJSONByteLength, featureTableBinaryByteLength,
batchTableJSONByteLength, batchTableBinaryByteLength.
"""
import json
import struct

HEADER_SIZE = 28
MAGIC = b"b3dm"


def batch_table_end_offset(data: bytes) -> int:
    """Byte offset where the batch table (and thus everything we need) ends."""
    (_version, _byte_length, ft_json_len, ft_bin_len, bt_json_len, bt_bin_len) = struct.unpack(
        "<6I", data[4:28]
    )
    return HEADER_SIZE + ft_json_len + ft_bin_len + bt_json_len + bt_bin_len


def extract_glb(data: bytes) -> bytes:
    """Extracts the embedded GLB payload from a full B3DM byte string."""
    offset = batch_table_end_offset(data)
    glb = data[offset:]
    if glb[:4] != b"glTF":
        raise ValueError(f"Invalid GLB magic at offset {offset}: {glb[:4]!r}")
    return glb


def extract_batch_table(data: bytes) -> dict:
    """Parses a B3DM header prefix and returns the batch table dict.

    `data` only needs to cover the header + tables (a small HTTP Range read
    suffices) — the mesh/GLB payload after this point is never touched.
    """
    if data[:4] != MAGIC:
        raise ValueError(f"Invalid B3DM magic: {data[:4]!r}")

    (_version, _byte_length, ft_json_len, ft_bin_len, bt_json_len, _bt_bin_len) = struct.unpack(
        "<6I", data[4:28]
    )

    bt_start = HEADER_SIZE + ft_json_len + ft_bin_len
    bt_end = bt_start + bt_json_len
    if bt_json_len == 0:
        return {}
    if len(data) < bt_end:
        raise ValueError(
            f"Fetched range too short for batch table: have {len(data)} bytes, need {bt_end}"
        )
    raw = data[bt_start:bt_end].decode("utf-8").rstrip("\x00")
    return json.loads(raw) if raw.strip() else {}
