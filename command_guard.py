"""Lightweight per-group command cooldowns."""

import asyncio
from time import monotonic

COMMAND_COOLDOWN_SECONDS = 60.0

_last_used: dict[tuple[str, str], float] = {}
_lock = asyncio.Lock()


async def claim_group_command(command: str, group_id: int | str) -> bool:
    """Claim one command slot, returning false while it is cooling down."""
    key = (command, str(group_id))
    now = monotonic()
    async with _lock:
        last_used = _last_used.get(key)
        if last_used is not None and now - last_used < COMMAND_COOLDOWN_SECONDS:
            return False
        _last_used[key] = now
        return True


def reset_command_cooldowns() -> None:
    """Clear process-local cooldown state (primarily for isolated tests)."""
    _last_used.clear()
