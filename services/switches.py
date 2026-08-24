"""Optional integration with the standalone global lock plugin."""

from collections.abc import Callable
from importlib import import_module


def _lock_reader() -> Callable[[int | str], bool] | None:
    parts = __package__.split(".")
    prefix = ".".join(parts[:-2])
    module_name = f"{prefix}.lock" if prefix else "lock"
    try:
        reader = import_module(module_name).is_master_on
    except (ImportError, AttributeError):
        return None
    if callable(reader):
        return reader
    return None


def is_master_on(group_id: int | str) -> bool:
    """Respect the global lock when installed; otherwise default to enabled."""

    reader = _lock_reader()
    return bool(reader(group_id)) if reader is not None else True
