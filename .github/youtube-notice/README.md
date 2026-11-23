# YouTube WebSub Notice (gozaru)

This directory documents the callback endpoint used by cogs/test/youtube.py. The aiohttp listener runs inside the Discord bot process; a second service is not required.

## Environment

Add these values to ci/.env. Never commit real API keys.

    YOUTUBE_API_KEY=your_youtube_data_api_v3_key
    YOUTUBE_CALLBACK_URL=https://y-api.suiten.site
    YOUTUBE_WEB_HOST=127.0.0.1
    YOUTUBE_WEB_PORT=8765
    YOUTUBE_DB_PATH=data/youtube.sqlite3

Configure Cloudflare Tunnel to route y-api.suiten.site to http://127.0.0.1:8765. The public callback is https://y-api.suiten.site/websub.

Merge this ingress rule into the existing tunnel configuration; do not replace existing ingress rules:

    - hostname: y-api.suiten.site
      service: http://127.0.0.1:8765

## Discord setup

1. Give the bot View Channel, Send Messages, Embed Links, Send Messages in Threads, and Manage Threads permissions in the destination Forum Channel.
2. Create optional Forum tags for upcoming streams, live streams, and new videos.
3. Run /youtubectrl setforum and select the Forum Channel.
4. Run /youtubectrl tags with tag names or IDs. Leave unused tag arguments blank.
5. Run /youtubectrl add with a YouTube channel ID beginning with UC.

The command group requires Manage Server permission. Configuration is stored in data/youtube.sqlite3.

## Delivery guarantees

WebSub is push-based, but it is not a hard real-time or exactly-once delivery system. Network outages, YouTube delivery delays, bot downtime, or missed feed updates can delay or lose an event. The implementation renews subscriptions every 72 hours, deduplicates forum threads with SQLite, and reconciles each channel's uploads playlist every 30 minutes. Known upcoming videos are also rechecked during reconciliation. This reduces the impact of missed pushes but cannot guarantee zero-miss delivery or instant detection. A first reconciliation may create threads for up to the 10 most recent uploads on each newly configured channel.

For six channels, steady-state reconciliation uses approximately 576 quota units per day (one playlistItems.list and one batched videos.list request per channel every 30 minutes), plus the one-time channels.list lookup per channel. Actual use varies with channel count and retries. YouTube Data API v3 calls consume quota. The API key belongs in the local ci/.env and must not be committed. The bot listener and Discord bot share one process and one SQLite database; do not run multiple bot instances against the same database.