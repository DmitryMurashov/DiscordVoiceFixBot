import discord
from discord.ext import commands

from src.core.discord import CustomVoiceClient

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!=', intents=intents)

index = 0


@bot.command()
async def join(ctx):  # Join channel
    channel = ctx.guild.voice_channels[index]
    await channel.connect(
        cls=CustomVoiceClient,
        timeout=15.0
    )


@bot.command()
async def move(ctx):  # Move to the next channel
    global index

    index = (index + 1) % len(ctx.guild.voice_channels)
    await ctx.guild.voice_client.move_to(ctx.guild.voice_channels[index])


@bot.command()
async def leave(ctx):  # Leave channel
    await ctx.guild.voice_client.disconnect()


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')


bot.run("Your token")
