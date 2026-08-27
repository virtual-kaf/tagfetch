import pytest
from nonebot_plugin_tagfetch import command_guard


@pytest.fixture(autouse=True)
def clear_cooldowns():
    command_guard.reset_command_cooldowns()
    yield
    command_guard.reset_command_cooldowns()


@pytest.mark.asyncio
async def test_artworks_cooldown_is_per_group(monkeypatch):
    now = 100.0
    monkeypatch.setattr(command_guard, "monotonic", lambda: now)

    assert await command_guard.claim_group_command("artworks", 100)
    assert not await command_guard.claim_group_command("artworks", 100)
    assert await command_guard.claim_group_command("artworks", 200)


@pytest.mark.asyncio
async def test_artworks_cooldown_reopens_after_sixty_seconds(monkeypatch):
    timestamps = iter((100.0, 159.999, 160.0))
    monkeypatch.setattr(command_guard, "monotonic", lambda: next(timestamps))

    assert await command_guard.claim_group_command("artworks", 100)
    assert not await command_guard.claim_group_command("artworks", 100)
    assert await command_guard.claim_group_command("artworks", 100)
