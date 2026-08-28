# tagfetch remote Grok service

This standalone authenticated service performs only Grok hashtag discovery. It
never imports QQ delivery state, calls twitterapi.io, or performs a fallback.
The QQ-side plugin owns the fallback policy and its persistent failure counter.

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
query contract used by the QQ plugin: tags, minimum 300 likes, a 24-hour
lookback, and at most three URLs per tag. Grok or schema failures return an
error response.

On the QQ side, connection failures and HTTP 5xx responses are counted across
polls. Every third consecutive failure consumes one twitterapi.io advanced
search attempt, but only from 08:00 through 23:59 China Standard Time. A Grok
success resets the counter. Disabled, invalid, or unauthorized remote-service
configuration does not trigger the paid fallback.

Configure the fallback only on the QQ/tagfetch host (the same names are used by
xfetch, so one key/base setting can be shared):

```dotenv
KABUBU_TWITTERAPI_IO_API_KEY=replace-with-a-twitterapi-io-key
# Optional: KABUBU_TWITTERAPI_IO_API_BASE=https://api.twitterapi.io
```
