"""Group command for the independent tagfetch switch."""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg

from .command_guard import claim_group_command
from .services.switches import is_master_on
from .storage import is_group_enabled, set_group_enabled

_ARTWORKS_ARGS = CommandArg()

artworks_cmd = on_command(
    "artworks",
    aliases={"美术部"},
    permission=GROUP_ADMIN | GROUP_OWNER,
    priority=1,
    block=True,
)


@artworks_cmd.handle()
async def handle_artworks_switch(
    event: GroupMessageEvent, args: Message = _ARTWORKS_ARGS
) -> None:
    if not await claim_group_command("artworks", event.group_id):
        await artworks_cmd.finish()
    action = args.extract_plain_text().strip().casefold()
    group_id = str(event.group_id)
    if action not in {"on", "off"}:
        enabled = is_group_enabled(group_id)
        master = is_master_on(group_id)
        await artworks_cmd.finish(
            "用法：/artworks on | off\n"
            f"本群作品推送：{'已开启' if enabled else '已关闭'}\n"
            f"Kabubu 总开关：{'已开启' if master else '已关闭'}"
        )
    enabled = action == "on"
    set_group_enabled(group_id, enabled)
    await artworks_cmd.finish(f"本群作品推送已{'开启' if enabled else '关闭'}。")
