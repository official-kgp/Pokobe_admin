import discord
from discord.ext import commands

import io
import textwrap
import traceback
from contextlib import redirect_stdout


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._last_result = None

    def cleanup_code(self, content):
        if content.startswith("```") and content.endswith("```"):
            return "\n".join(content.split("\n")[1:-1])
        return content

    @commands.command(name="eval", hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def _eval(self, ctx, *, body: str):
        body = self.cleanup_code(body)

        env = {
            "bot": self.bot,
            "ctx": ctx,
            "discord": discord,
            "commands": commands,
            "guild": ctx.guild,
            "channel": ctx.channel,
            "author": ctx.author,
            "message": ctx.message,
            "_": self._last_result,
            "__import__": __import__,
        }

        env.update(globals())

        stdout = io.StringIO()

        body = textwrap.indent(body, "    ")

        code = f"""
async def func():
{body}
"""

        try:
            exec(code, env)
        except Exception as e:
            return await ctx.send(f"```py\n{traceback.format_exc()}\n```")

        func = env["func"]

        try:
            with redirect_stdout(stdout):
                ret = await func()

        except Exception:
            value = stdout.getvalue()
            await ctx.send(
                f"```py\n{value}{traceback.format_exc()}\n```"
            )

        else:
            value = stdout.getvalue()

            try:
                await ctx.message.add_reaction("✅")
            except discord.HTTPException:
                pass

            if ret is None:
                if value:
                    await ctx.send(f"```py\n{value}\n```")
            else:
                self._last_result = ret
                await ctx.send(f"```py\n{value}{ret}\n```")


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
