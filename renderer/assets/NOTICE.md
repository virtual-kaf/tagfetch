# Renderer resources

`HYSongYunLangHeiW.ttf` is copied from the MIT-licensed
[`nonebot-plugin-parser`](https://github.com/fllesser/nonebot-plugin-parser)
renderer resources so this plugin can render CJK text without relying on host
fonts. The surrounding Pillow layout is an independent Tagfetch implementation
and does not import xfetch or `nonebot-plugin-parser` at runtime.
