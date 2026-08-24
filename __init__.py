"""Popular hashtag discovery and fail-closed QQ delivery plugin."""

from nonebot import get_driver

try:
    get_driver()
except ValueError:
    __all__: list[str] = []
else:
    from nonebot.plugin import PluginMetadata

    from .commands import tagfetch_cmd
    from .renderer import shutdown as shutdown_renderer
    from .scheduler import check_tagfetch
    from .storage import initialize_database

    initialize_database()

    get_driver().on_shutdown(shutdown_renderer)
    __plugin_meta__ = PluginMetadata(
        name="tagfetch",
        description="安全发现并推送热门 hashtag 推文",
        usage="/kabubu tagfetch on | off | status",
        type="application",
        supported_adapters={"~onebot.v11"},
    )

    __all__ = ["check_tagfetch", "tagfetch_cmd"]
