"""Runtime compatibility helpers for dfindexeddb on macOS."""

from __future__ import annotations

import sys
import types


def install_snappy_shim() -> None:
    """Provide the small python-snappy API dfindexeddb requires via cramjam."""
    if "snappy" in sys.modules:
        return

    import cramjam

    module = types.ModuleType("snappy")

    class UncompressError(Exception):
        pass

    def decompress(data: bytes) -> bytes:
        try:
            return bytes(cramjam.snappy.decompress_raw(data))
        except Exception as exc:  # pragma: no cover - backend-specific
            raise UncompressError(str(exc)) from exc

    def compress(data: bytes) -> bytes:
        return bytes(cramjam.snappy.compress_raw(data))

    module.UncompressError = UncompressError
    module.decompress = decompress
    module.compress = compress
    sys.modules["snappy"] = module
