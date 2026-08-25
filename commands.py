"""Group command for the independent tagfetch switch."""

from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.params import CommandArg

from .config import ADMIN_IDS, SUPER_ADMIN_IDS
from .services.switches import is_master_on
from .storage import is_group_enabled, set_group_enabled

_ARTWORKS_ARGS = CommandArg()

artworks_cmd = on_command(
    "kabubu artworks",
    priority=1,
    block=True,
)


def _configured_admin_ids() -> set[int]:
    return set(ADMIN_IDS | SUPER_ADMIN_IDS)


def _is_authorized(event: GroupMessageEvent) -> bool:
    if event.sender.role in {"owner", "admin"}:
        return True
    if int(event.user_id) in _configured_admin_ids():
        return True
    try:
        superusers = {str(user_id) for user_id in get_driver().config.superusers}
    except (AttributeError, TypeError):
        superusers = set()
    return str(event.user_id) in superusers


@artworks_cmd.handle()
async def handle_artworks_switch(
    event: GroupMessageEvent, args: Message = _ARTWORKS_ARGS
) -> None:
    action = args.extract_plain_text().strip().casefold()
    group_id = str(event.group_id)
    if action not in {"on", "off"}:
        enabled = is_group_enabled(group_id)
        master = is_master_on(group_id)
        await artworks_cmd.finish(
            "用法：/kabubu artworks on | off\n"
            f"本群作品推送：{'已开启' if enabled else '已关闭'}\n"
            f"Kabubu 总开关：{'已开启' if master else '已关闭'}"
        )
    if not _is_authorized(event):
        await artworks_cmd.finish("只有群主、群管理员或机器人管理员可以切换作品推送")
    enabled = action == "on"
    set_group_enabled(group_id, enabled)
    await artworks_cmd.finish(f"本群作品推送已{'开启' if enabled else '关闭'}。")
