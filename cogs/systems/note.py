import os
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv(dotenv_path="ci/.env")

NOTE_WEBHOOK_URL = os.getenv("NOTE_WEBHOOK_URL")
SERVER_OWNER_ID = int(os.getenv("SERVER_OWNER_ID", "0"))
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0"))

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_DESCRIPTION_LENGTH = 900


class NoteMetadataParser(HTMLParser):
    """note.com の Open Graph メタデータを取得する簡易パーサー。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return

        values = {key.lower(): value for key, value in attrs if key and value}
        key = values.get("property") or values.get("name")
        value = values.get("content")

        if key and value:
            self.metadata.setdefault(key.lower(), value.strip())


def is_note_article_url(url: str) -> bool:
    """note.com の記事 URL だけを許可する。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and (parsed.hostname == "note.com" or parsed.hostname.endswith(".note.com"))
        and parsed.path.startswith("/")
    )


def clean_description(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value or "")
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > MAX_DESCRIPTION_LENGTH:
        value = value[: MAX_DESCRIPTION_LENGTH - 1].rstrip() + "…"
    return value or "記事のプレビューを取得できませんでした。"


async def fetch_note_metadata(url: str) -> dict[str, str]:
    """記事ページの og:description と og:image を取得する。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; KGPNotePublisher/1.0; "
            "+https://note.com/official_kgp)"
        )
    }

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()

            # リダイレクト先も note.com 以外にはアクセスしない。
            if not is_note_article_url(str(response.url)):
                raise ValueError("note.com 以外の URL へリダイレクトされました。")

            html = await response.text(errors="replace")

    parser = NoteMetadataParser()
    parser.feed(html)
    metadata = parser.metadata

    description = (
        metadata.get("og:description")
        or metadata.get("description")
        or metadata.get("twitter:description")
        or ""
    )
    image = metadata.get("og:image") or metadata.get("twitter:image") or ""

    return {
        "description": clean_description(description),
        "image": image,
    }


class NotePostView(discord.ui.LayoutView):
    """note告知レイアウト。リンクボタンのみなので操作状態の復元は不要。"""

    def __init__(self, article_url: str, description: str, image_url: str | None):
        super().__init__(timeout=None)

        container = discord.ui.Container()
        container.add_item(
            discord.ui.TextDisplay("## 🔴 最新Noteが更新されました！")
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(description))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"[記事を読む]({article_url})"))
        container.add_item(discord.ui.TextDisplay("\u200b"))

        if image_url and image_url.startswith(("https://", "http://")):
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media=image_url)
                )
            )

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="View on note",
                    style=discord.ButtonStyle.link,
                    url=article_url,
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                f"-# ©{datetime.now().year} かざま隊の集いの場"
            )
        )

        self.add_item(container)


class NotePublisherCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        if not NOTE_WEBHOOK_URL:
            print("⚠️ NOTE_WEBHOOK_URL が未設定のため、NotePublisher は送信できません。")

    async def cog_check(self, ctx: commands.Context) -> bool:
        """サーバーオーナーまたは MOD_ROLE_ID を持つメンバーに限定する。"""
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return False

        if ctx.author.id == SERVER_OWNER_ID or ctx.author.id == ctx.guild.owner_id:
            return True

        return MOD_ROLE_ID != 0 and any(
            role.id == MOD_ROLE_ID for role in ctx.author.roles
        )

    @commands.command(name="note")
    @commands.guild_only()
    async def publish_note(self, ctx: commands.Context, url: str):
        """note.com の記事 URL を受け取り、Webhook で告知する。"""
        if not NOTE_WEBHOOK_URL:
            await ctx.reply("❌ NOTE_WEBHOOK_URL が設定されていません。", mention_author=False)
            return

        if not is_note_article_url(url):
            await ctx.reply(
                "❌ note.com の記事 URL を指定してください。\n"
                "例: P!note https://note.com/official_kgp/n/記事ID",
                mention_author=False,
            )
            return

        await ctx.typing()

        try:
            metadata = await fetch_note_metadata(url)
        except (aiohttp.ClientError, ValueError) as exc:
            await ctx.reply(
                f"❌ 記事情報を取得できませんでした: {type(exc).__name__}",
                mention_author=False,
            )
            return
        except Exception as exc:
            await ctx.reply(
                f"❌ 記事情報の処理中にエラーが発生しました: {type(exc).__name__}",
                mention_author=False,
            )
            return

        try:
            webhook = discord.Webhook.from_url(NOTE_WEBHOOK_URL, client=self.bot)
            view = NotePostView(
                article_url=url,
                description=metadata["description"],
                image_url=metadata["image"],
            )
            await webhook.send(
                view=view,
                username="かざま隊の集いの場 note編集者",
                allowed_mentions=discord.AllowedMentions.none(),
                wait=True,
            )
        except (discord.HTTPException, ValueError) as exc:
            await ctx.reply(
                f"❌ Webhook 送信に失敗しました: {type(exc).__name__}",
                mention_author=False,
            )
            return

        await ctx.message.add_reaction("✅")


async def setup(bot: commands.Bot):
    await bot.add_cog(NotePublisherCog(bot))
