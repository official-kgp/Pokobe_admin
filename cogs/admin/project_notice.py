import discord
from discord import ui
from discord.ext import commands


NOTE_URL = "https://note.com/official_kgp/n/nd609c1aa41ab"
ANNOUNCEMENT_ROLE_ID = 1363454584919691284


class ProjectNoticeView(ui.LayoutView):
    """Persistent Components V2 notice with Japanese/English switching."""

    def __init__(self, language: str = "ja"):
        super().__init__(timeout=None)
        self.language = language

        if language == "en":
            body = (
                "Hello everyone in Kazamatai's community! "
                "This is Pokobee, the server administrator.\n\n"
                "As our server membership has grown, we have decided to "
                "review various aspects of the server. The staff team has "
                "an announcement to share.\n\n"
                "Our server is moving into its next phase!\n\n"
                "We've explained in detail on note what will change going "
                "forward. Please take a look!\n\n"
                f"{NOTE_URL}\n\n"
                f"<@&{ANNOUNCEMENT_ROLE_ID}>"
            )
            ja_label = "🇯🇵 日本語"
            en_label = "🇬🇧 English"
        else:
            body = (
                "かざま隊の集いの場にご参加の皆さん、こんにちは！"
                "ぽこべぇ管理人です。\n\n"
                "サーバーメンバーが増えてきたこともあり、"
                "当サーバーの色々な点を見直す必要があると判断しました。"
                "そこで、運営よりお知らせがあります。\n\n"
                "当サーバーは次のフェーズへと進化します！\n\n"
                "今後どのような点が変化していくのか、"
                "詳しくnoteでお伝えしています。是非見てください！\n\n"
                f"{NOTE_URL}\n\n"
                f"<@&{ANNOUNCEMENT_ROLE_ID}>"
            )
            ja_label = "🇯🇵 日本語"
            en_label = "🇬🇧 English"

        self.add_item(
            ui.TextDisplay(
                "## \u2003\u2003KGP ReNewal Project\n"
                "## ──Project NEXUS──"
            )
        )
        self.add_item(
            ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.large,
            )
        )
        self.add_item(ui.TextDisplay(body))
        self.add_item(
            ui.ActionRow(
                self.LanguageButton(
                    label=ja_label,
                    custom_id="kgp:renewal:language:ja",
                    language="ja",
                ),
                self.LanguageButton(
                    label=en_label,
                    custom_id="kgp:renewal:language:en",
                    language="en",
                ),
            )
        )

    class LanguageButton(ui.Button):
        def __init__(self, *, label: str, custom_id: str, language: str):
            super().__init__(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=custom_id,
            )
            self.language = language

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=ProjectNoticeView(language=self.language),
                allowed_mentions=discord.AllowedMentions.none(),
            )


class ProjectNoticeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="project_notice", hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def project_notice(self, ctx: commands.Context):
        """Send the KGP Renewal Project announcement (owner only)."""
        await ctx.send(
            view=ProjectNoticeView(language="ja"),
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot):
    # Register the persistent view on every bot startup.
    bot.add_view(ProjectNoticeView(language="ja"))
    await bot.add_cog(ProjectNoticeCog(bot))
