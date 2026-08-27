from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def test_artworks_command_and_alias_are_exact():
    commands = (PLUGIN_DIR / "commands.py").read_text(encoding="utf-8")

    assert '"artworks",\n    aliases={"美术部"}' in commands
    assert "permission=GROUP_ADMIN | GROUP_OWNER" in commands
    assert '"kabubu artworks"' not in commands
    assert "ADMIN_IDS" not in commands
    assert "SUPER_ADMIN_IDS" not in commands
    assert "get_driver" not in commands
