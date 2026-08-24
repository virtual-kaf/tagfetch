"""Two-hour CST scheduler for the tagfetch pipeline."""

from nonebot import get_bot, logger, require

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from .config import CST, TAGS
from .core import run_tagfetch_pipeline
from .services import broadcast_to_groups, is_master_on
from .storage import get_enabled_group_ids


async def get_active_group_ids(bot) -> list[str]:
    configured = set(get_enabled_group_ids())
    if not configured:
        return []
    try:
        group_list = await bot.get_group_list()
    except Exception:  # noqa: BLE001 - adapter failures suppress this poll
        logger.exception("[TagfetchScheduler] failed to read bot group list")
        return []
    joined = {str(group["group_id"]) for group in group_list}
    return sorted(
        group_id
        for group_id in configured & joined
        if is_master_on(group_id)
    )


@scheduler.scheduled_job(
    "cron",
    hour="*/2",
    minute="30",
    timezone=CST,
    id="tagfetch_monitor",
    max_instances=1,
    coalesce=True,
)
async def check_tagfetch() -> None:
    if not TAGS:
        logger.warning("[TagfetchScheduler] KABUBU_TAGFETCH_TAGS is empty")
        return
    try:
        bot = get_bot()
    except Exception:  # noqa: BLE001 - NoneBot may expose adapter-specific errors
        logger.warning("[TagfetchScheduler] bot is unavailable")
        return
    group_ids = await get_active_group_ids(bot)
    if not group_ids:
        logger.debug("[TagfetchScheduler] no enabled groups; skip remote request")
        return
    try:
        candidates = await run_tagfetch_pipeline(TAGS, group_ids)
        await broadcast_to_groups(bot, candidates, group_ids)
    except Exception:  # noqa: BLE001 - scheduler boundary must not leak failures
        logger.exception("[TagfetchScheduler] scheduled run failed")