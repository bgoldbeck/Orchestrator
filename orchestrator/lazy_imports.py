"""Import-time shortcuts for a faster first window.

qfluentwidgets imports ``qfluentwidgets.common.image_utils`` at package import time only to get
``gaussianBlur`` for acrylic effects. That module pulls in numpy, PIL, colorthief and
scipy.ndimage (~200 ms) although the app never blurs anything during startup. ``defer_qfw_image_utils``
puts a stand-in module there whose ``gaussianBlur`` loads the real module on first call, so acrylic
still works if something ever uses it.
"""
from __future__ import annotations

import importlib
import sys
import types

_IMAGE_UTILS = "qfluentwidgets.common.image_utils"


def _load_real_image_utils():
    mod = sys.modules.get(_IMAGE_UTILS)
    if mod is not None and not getattr(mod, "_orchestrator_stub", False):
        return mod
    sys.modules.pop(_IMAGE_UTILS, None)  # drop the stand-in so the normal import finds the real module
    try:
        return importlib.import_module(_IMAGE_UTILS)
    except Exception:
        if mod is not None:  # keep the stand-in so later calls fail the same clear way
            sys.modules.setdefault(_IMAGE_UTILS, mod)
        raise


def defer_qfw_image_utils() -> None:
    """Call before the first ``import qfluentwidgets``. No-op if it was already imported."""
    if _IMAGE_UTILS in sys.modules or "qfluentwidgets" in sys.modules:
        return
    stub = types.ModuleType(_IMAGE_UTILS)
    stub._orchestrator_stub = True  # type: ignore[attr-defined]

    def gaussianBlur(*args, **kwargs):  # noqa: N802 - same name as qfluentwidgets'
        return _load_real_image_utils().gaussianBlur(*args, **kwargs)

    def __getattr__(name: str):  # anything else (DominantColor, fromqpixmap, ...) loads the real module
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(_load_real_image_utils(), name)

    stub.gaussianBlur = gaussianBlur  # type: ignore[attr-defined]
    stub.__getattr__ = __getattr__  # type: ignore[attr-defined]
    sys.modules[_IMAGE_UTILS] = stub
