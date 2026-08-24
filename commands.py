"""Group command for the independent tagfetch switch."""

from nonebot import get_driver, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.params import CommandArg

from .services.switches import is_master_on
from .storage import is_group_enabled, set_group_enabled

_TAGFETCH_ARGS = CommandArg()

tagfetch_cmd = on_command(
    "kabubu tagfetch",
    aliases={"/kabubu tagfetch", "tagfetch", "/tagfetch"},
    priority=1,
    block=True,
)


def _configured_admin_ids() -> set[int]:
    try:
        from nonebot_plugin_kabubu_chat.config import get_config

        config = get_config()
        return {
            *map(int, config.kabubu_admin_list),
            *map(int, config.kabubu_super_admin),
        }
    except (AttributeError, ImportError, TypeError, ValueError):
        return set()


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


@tagfetch_cmd.handle()
async def handle_tagfetch_switch(
    event: GroupMessageEvent, args: Message = _TAGFETCH_ARGS
) -> None:
    action = args.extract_plain_text().strip().casefold()
    group_id = str(event.group_id)
    if action in {"", "status", "状态"}:
        enabled = is_group_enabled(group_id)
        master = is_master_on(group_id)
        await tagfetch_cmd.finish(
            f"Tagfetch：{'已开启' if enabled else '已关闭'}\n"
            f"Kabubu 总开关：{'已开启' if master else '已关闭'}"
        )
    if action not in {"on", "off", "开启", "关闭"}:
        await tagfetch_cmd.finish("用法：/kabubu tagfetch on | off | status")
    if not _is_authorized(event):
        await tagfetch_cmd.finish("只有群主、群管理员或机器人管理员可以切换 tagfetch")
    enabled = action in {"on", "开启"}
    set_group_enabled(group_id, enabled)
    await tagfetch_cmd.finish(f"本群 Tagfetch 已{'开启' if enabled else '关闭'}。")
