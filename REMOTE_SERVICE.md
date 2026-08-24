# tagfetch remote Grok service

This standalone authenticated service performs only Grok hashtag discovery. It
never imports QQ delivery state, calls TwitterAPI, or performs a fallback.

## Configuration

```dotenv
KABUBU_TAGFETCH_REMOTE_ENABLED=true
KABUBU_TAGFETCH_REMOTE_HOST=100.98.44.83
KABUBU_TAGFETCH_REMOTE_PORT=8766
KABUBU_TAGFETCH_REMOTE_TOKEN=replace-with-a-long-random-token
KABUBU_GROK_API_KEY=replace-with-the-grok-token
# Optional: KABUBU_GROK_API_URL=http://127.0.0.1:8000/v1/chat/completions
# Optional: KABUBU_GROK_MODEL=grok-4.20-0309-non-reasoning
```

Start it with the project Python environment:

```powershell
python -m nonebot_plugin_tagfetch.remote_service
```

The endpoint is `POST /api/tagfetch/poll`. It accepts the authenticated fixed
query contract used by the QQ plugin: tags, minimum 300 likes, a two-hour
lookback, and at most three URLs per tag. Grok or schema failures return an
error response; callers must not fall back to another discovery source.