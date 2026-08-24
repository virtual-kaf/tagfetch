"""Tweet URL validation owned by Tagfetch."""

import re

TWEET_URL_RE = re.compile(
    r"^https?://(?:www\.)?"
    r"(?:twitter\.com|x\.com|fxtwitter\.com|fixupx\.com|twittpr\.com)"
    r"/([^/?#]+)/status/([0-9]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
