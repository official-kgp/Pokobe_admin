from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv(dotenv_path="ci/.env")
log = logging.getLogger("gozaru.youtube")
HUB = "https://pubsubhubbub.appspot.com/subscribe"
NS = {"yt": "http://www.youtube.com/xml/schemas/2015", "atom": "http://www.w3.org/2005/Atom"}
CHANNEL_RE = re.compile(r"^UC[a-zA-Z0-9_-]{22}$")
VIDEO_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
DB_PATH = Path(os.getenv("YOUTUBE_DB_PATH", "data/youtube.sqlite3"))
CALLBACK = os.getenv("YOUTUBE_CALLBACK_URL", "").rstrip("/")
API_KEY = os.getenv("YOUTUBE_API_KEY", "")
HOST = os.getenv("YOUTUBE_WEB_HOST", "127.0.0.1")
PORT = int(os.getenv("YOUTUBE_WEB_PORT", "8765"))


def now():
    return datetime.now(timezone.utc).isoformat()


class YouTubeNoticeView(discord.ui.LayoutView):
    def __init__(self, video_id: str, title: str, channel: str, status: str):
        super().__init__(timeout=None)
        url = f"https://www.youtube.com/watch?v={video_id}"
        heading = {"upcoming": "🔔 配信予定", "live": "🔴 ライブ配信中", "video": "🎬 新着動画"}.get(status, "YouTube更新")
        box = discord.ui.Container()
        box.add_item(discord.ui.TextDisplay(f"## {heading}"))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(f"**{discord.utils.escape_markdown(title)[:240]}**"))
        box.add_item(discord.ui.TextDisplay(f"チャンネル：{discord.utils.escape_markdown(channel)[:100]}"))
        box.add_item(discord.ui.TextDisplay(f"[YouTubeで開く]({url})"))
        box.add_item(discord.ui.ActionRow(discord.ui.Button(label="YouTubeを開く", style=discord.ButtonStyle.link, url=url)))
        box.add_item(discord.ui.TextDisplay("-# かざま隊の集いの場 · YouTube通知"))
        self.add_item(box)


class YouTubeNotice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = None
        self.session = None
        self.runner = None
        self.lock = asyncio.Lock()

    async def cog_load(self):
        from aiohttp import web
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS guild_config(
            guild_id INTEGER PRIMARY KEY, forum_id INTEGER NOT NULL,
            upcoming_tag_id INTEGER, live_tag_id INTEGER, video_tag_id INTEGER);
        CREATE TABLE IF NOT EXISTS tracked_channels(
            guild_id INTEGER NOT NULL, channel_id TEXT NOT NULL,
            PRIMARY KEY(guild_id, channel_id));
        CREATE TABLE IF NOT EXISTS videos(
            video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, title TEXT NOT NULL,
            channel_title TEXT, status TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries(
            guild_id INTEGER NOT NULL, video_id TEXT NOT NULL, thread_id INTEGER NOT NULL,
            PRIMARY KEY(guild_id, video_id));
        """)
        self.db.commit()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        app = web.Application(client_max_size=262144)
        app.router.add_get("/websub", self.verify)
        app.router.add_post("/websub", self.receive)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, HOST, PORT).start()
        self.renew.start()

    async def cog_unload(self):
        self.renew.cancel()
        if self.runner:
            await self.runner.cleanup()
        if self.session and not self.session.closed:
            await self.session.close()
        if self.db:
            self.db.close()

    def q(self, sql, args=()):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    @staticmethod
    def topic_channel(topic):
        m = re.search(r"(?:\\?|&)channel_id=(UC[a-zA-Z0-9_-]{22})(?:&|$)", topic)
        return m.group(1) if m else None

    async def verify(self, request):
        from aiohttp import web
        mode = request.query.get("hub.mode")
        challenge = request.query.get("hub.challenge")
        channel_id = self.topic_channel(request.query.get("hub.topic", ""))
        if mode not in ("subscribe", "unsubscribe") or not challenge or not channel_id:
            raise web.HTTPBadRequest(text="invalid WebSub challenge")
        if mode == "subscribe" and not self.q("SELECT 1 FROM tracked_channels WHERE channel_id=? LIMIT 1", (channel_id,)).fetchone():
            raise web.HTTPForbidden(text="channel not configured")
        return web.Response(text=challenge, content_type="text/plain")

    async def receive(self, request):
        from aiohttp import web
        try:
            root = ET.fromstring(await request.read())
        except ET.ParseError:
            raise web.HTTPBadRequest(text="invalid Atom XML")
        entry = root.find("atom:entry", NS)
        if entry is None:
            return web.Response(status=204)
        vid = (entry.findtext("yt:videoId", "", NS) or "").strip()
        cid = (entry.findtext("yt:channelId", "", NS) or "").strip()
        title = entry.findtext("atom:title", "YouTube update", NS) or "YouTube update"
        if not VIDEO_RE.fullmatch(vid) or not CHANNEL_RE.fullmatch(cid):
            raise web.HTTPBadRequest(text="invalid video/channel ID")
        if self.q("SELECT 1 FROM tracked_channels WHERE channel_id=? LIMIT 1", (cid,)).fetchone():
            asyncio.create_task(self.process(vid, cid, title))
        return web.Response(status=204)

    async def subscribe(self, cid, mode="subscribe"):
        if not CALLBACK or not self.session:
            log.warning("YOUTUBE_CALLBACK_URL is missing")
            return False
        data = {
            "hub.callback": CALLBACK + "/websub",
            "hub.topic": f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={cid}",
            "hub.verify": "async", "hub.mode": mode,
        }
        if mode == "subscribe":
            data["hub.lease_seconds"] = "864000"
        async with self.session.post(HUB, data=data) as r:
            if r.status not in (202, 204):
                log.error("WebSub subscribe failed: %s (%s)", cid, r.status)
                return False
        return True

    @tasks.loop(hours=72)
    async def renew(self):
        await self.bot.wait_until_ready()
        for row in self.q("SELECT DISTINCT channel_id FROM tracked_channels").fetchall():
            try:
                await self.subscribe(row["channel_id"])
                await asyncio.sleep(0.2)
            except Exception:
                log.exception("WebSub renewal error")

    @renew.before_loop
    async def before_renew(self):
        await self.bot.wait_until_ready()

    async def fetch_video(self, vid):
        if not API_KEY or not self.session:
            log.error("YOUTUBE_API_KEY is missing")
            return None
        params = {"part": "snippet", "id": vid, "key": API_KEY}
        async with self.session.get("https://www.googleapis.com/youtube/v3/videos", params=params) as r:
            if r.status != 200:
                log.error("videos.list returned HTTP %s", r.status)
                return None
            data = await r.json()
        items = data.get("items") or []
        if not items:
            return None
        snippet = items[0].get("snippet", {})
        live = snippet.get("liveBroadcastContent", "none")
        return {
            "id": vid, "channel_id": snippet.get("channelId"),
            "title": snippet.get("title") or "タイトル未取得",
            "channel": snippet.get("channelTitle") or "YouTube",
            "status": live if live in ("upcoming", "live") else "video",
        }

    async def process(self, vid, cid, feed_title):
        async with self.lock:
            item = await self.fetch_video(vid)
            if not item or item["channel_id"] != cid:
                return
            previous = self.q("SELECT status FROM videos WHERE video_id=?", (vid,)).fetchone()
            old = previous["status"] if previous else None
            self.q("""INSERT INTO videos VALUES(?,?,?,?,?,?)
                ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,
                channel_title=excluded.channel_title,status=excluded.status,updated_at=excluded.updated_at""",
                (vid, cid, item["title"], item["channel"], item["status"], now()))
            configs = self.q("""SELECT tc.guild_id,gc.forum_id,gc.upcoming_tag_id,gc.live_tag_id,gc.video_tag_id
                FROM tracked_channels tc JOIN guild_config gc ON gc.guild_id=tc.guild_id
                WHERE tc.channel_id=?""", (cid,)).fetchall()
            for cfg in configs:
                existing = self.q("SELECT thread_id FROM deliveries WHERE guild_id=? AND video_id=?", (cfg["guild_id"], vid)).fetchone()
                if existing:
                    if old == "upcoming" and item["status"] == "live":
                        await self.update_live(cfg, existing["thread_id"], item)
                else:
                    await self.create_thread(cfg, item)

    async def create_thread(self, cfg, item):
        guild = self.bot.get_guild(cfg["guild_id"])
        forum = guild.get_channel(cfg["forum_id"]) if guild else None
        if not isinstance(forum, discord.ForumChannel):
            return
        status = item["status"]
        tag_id = cfg["upcoming_tag_id"] if status == "upcoming" else cfg["live_tag_id"] if status == "live" else cfg["video_tag_id"]
        tags = [t for t in forum.available_tags if t.id == tag_id] if tag_id else []
        try:
            thread, _ = await forum.create_thread(
                name=discord.utils.escape_markdown(item["title"])[:90] or f"YouTube {item['id']}",
                view=YouTubeNoticeView(item["id"], item["title"], item["channel"], status),
                applied_tags=tags, reason="YouTube WebSub notification")
        except discord.HTTPException:
            log.exception("Forum thread creation failed for guild %s", cfg["guild_id"])
            return
        self.q("INSERT OR IGNORE INTO deliveries VALUES(?,?,?)", (cfg["guild_id"], item["id"], thread.id))

    async def update_live(self, cfg, thread_id, item):
        guild = self.bot.get_guild(cfg["guild_id"])
        thread = guild.get_thread(thread_id) if guild else None
        if thread is None and guild:
            try:
                thread = await guild.fetch_channel(thread_id)
            except discord.HTTPException:
                return
        if not isinstance(thread, discord.Thread):
            return
        try:
            await thread.send(view=YouTubeNoticeView(item["id"], item["title"], item["channel"], "live"))
            forum = guild.get_channel(cfg["forum_id"])
            tag_id = cfg["live_tag_id"]
            if isinstance(forum, discord.ForumChannel) and tag_id:
                live_tag = next((t for t in forum.available_tags if t.id == tag_id), None)
                if live_tag:
                    tags = [t for t in thread.applied_tags if t.id != cfg["upcoming_tag_id"]]
                    await thread.edit(applied_tags=[*tags, live_tag])
        except discord.HTTPException:
            log.exception("Live thread update failed: %s", thread_id)

    @commands.hybrid_group(name="youtubectrl", description="YouTube通知の設定", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def youtubectrl(self, ctx):
        await ctx.send("サブコマンド: setforum / add / remove / list / tags")

    @youtubectrl.command(name="setforum", description="通知先フォーラムを設定")
    async def setforum(self, ctx, forum: discord.ForumChannel):
        self.q("""INSERT INTO guild_config(guild_id,forum_id) VALUES(?,?)
            ON CONFLICT(guild_id) DO UPDATE SET forum_id=excluded.forum_id""", (ctx.guild.id, forum.id))
        await ctx.reply(f"通知先を {forum.mention} に設定しました。", ephemeral=True)

    @youtubectrl.command(name="add", description="監視チャンネルを追加")
    async def add(self, ctx, channel_id: str):
        cid = channel_id.strip()
        if not CHANNEL_RE.fullmatch(cid):
            await ctx.reply("UCから始まるYouTubeチャンネルIDを指定してください。", ephemeral=True)
            return
        if not self.q("SELECT 1 FROM guild_config WHERE guild_id=?", (ctx.guild.id,)).fetchone():
            await ctx.reply("先に /youtubectrl setforum を実行してください。", ephemeral=True)
            return
        self.q("INSERT OR IGNORE INTO tracked_channels VALUES(?,?)", (ctx.guild.id, cid))
        ok = await self.subscribe(cid)
        await ctx.reply("監視対象に追加しました。" + (" WebSub購読リクエスト送信済み。" if ok else " WebSub未送信。環境変数を確認してください。"), ephemeral=True)

    @youtubectrl.command(name="remove", description="監視チャンネルを削除")
    async def remove(self, ctx, channel_id: str):
        cid = channel_id.strip()
        self.q("DELETE FROM tracked_channels WHERE guild_id=? AND channel_id=?", (ctx.guild.id, cid))
        if not self.q("SELECT 1 FROM tracked_channels WHERE channel_id=? LIMIT 1", (cid,)).fetchone():
            await self.subscribe(cid, "unsubscribe")
        await ctx.reply("監視対象から削除しました。", ephemeral=True)

    @youtubectrl.command(name="list", description="監視設定を表示")
    async def list_channels(self, ctx):
        cfg = self.q("SELECT forum_id FROM guild_config WHERE guild_id=?", (ctx.guild.id,)).fetchone()
        rows = self.q("SELECT channel_id FROM tracked_channels WHERE guild_id=?", (ctx.guild.id,)).fetchall()
        lines = [f"通知先: {f'<#{cfg[\"forum_id\"]}>' if cfg else '未設定'}"]
        lines += [f"• {r['channel_id']}" for r in rows]
        await ctx.reply("\\n".join(lines)[:1900], ephemeral=True)

    @youtubectrl.command(name="tags", description="フォーラムタグを設定（名前またはID）")
    async def tags(self, ctx, upcoming: str = "", live: str = "", video: str = ""):
        cfg = self.q("SELECT forum_id FROM guild_config WHERE guild_id=?", (ctx.guild.id,)).fetchone()
        forum = ctx.guild.get_channel(cfg["forum_id"]) if cfg else None
        if not isinstance(forum, discord.ForumChannel):
            await ctx.reply("通知先フォーラムを先に設定してください。", ephemeral=True)
            return
        def find(value):
            if not value.strip():
                return None
            tag = next((t for t in forum.available_tags if t.name == value.strip()), None)
            if tag:
                return tag.id
            try:
                target = int(value)
            except ValueError:
                return -1
            return target if any(t.id == target for t in forum.available_tags) else -1
        values = [find(v) for v in (upcoming, live, video)]
        if -1 in values:
            await ctx.reply("フォーラムに存在するタグ名またはIDを指定してください。", ephemeral=True)
            return
        self.q("UPDATE guild_config SET upcoming_tag_id=?,live_tag_id=?,video_tag_id=? WHERE guild_id=?",
               (*values, ctx.guild.id))
        await ctx.reply("タグ設定を更新しました。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeNotice(bot))
