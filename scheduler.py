"""Two-hour CST scheduler for the tagfetch pipeline."""

from time import monotonic

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
        logger.info("[TagfetchScheduler] no group has enabled tagfetch")
        return []
    logger.info(
        "[TagfetchScheduler] checking configured groups count={}", len(configured)
    )
    try:
        group_list = await bot.get_group_list()
    except Exception:  # noqa: BLE001 - adapter failures suppress this poll
        logger.exception("[TagfetchScheduler] failed to read bot group list")
        return []
    joined = {str(group["group_id"]) for group in group_list}
    active = sorted(
        group_id for group_id in configured & joined if is_master_on(group_id)
    )
    logger.info(
        "[TagfetchScheduler] group filter configured={} joined={} active={} "
        "not_joined={} master_blocked={}",
        len(configured),
        len(configured & joined),
        len(active),
        len(configured - joined),
        len((configured & joined) - set(active)),
    )
    return active


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
    started = monotonic()
    logger.info("[TagfetchScheduler] poll started configured_tags={}", len(TAGS))
    if not TAGS:
        logger.warning(
            "[TagfetchScheduler] poll skipped reason=tags_empty "
            "setting=KABUBU_TAGFETCH_TAGS"
        )
        return
    try:
        bot = get_bot()
    except Exception:  # noqa: BLE001 - NoneBot may expose adapter-specific errors
        logger.warning("[TagfetchScheduler] poll skipped reason=bot_unavailable")
        return
    group_ids = await get_active_group_ids(bot)
    if not group_ids:
        logger.info(
            "[TagfetchScheduler] poll skipped reason=no_active_groups "
            "remote_request=false"
        )
        return
    try:
        logger.info(
            "[TagfetchScheduler] pipeline starting active_groups={}", len(group_ids)
        )
        candidates = await run_tagfetch_pipeline(TAGS, group_ids)
        logger.info(
            "[TagfetchScheduler] pipeline finished prepared_candidates={}",
            len(candidates),
        )
        await broadcast_to_groups(bot, candidates, group_ids)
    except Exception:  # noqa: BLE001 - scheduler boundary must not leak failures
        logger.exception("[TagfetchScheduler] scheduled run failed")
    finally:
        logger.info(
            "[TagfetchScheduler] poll finished elapsed_seconds={:.2f}",
            monotonic() - started,
        )
