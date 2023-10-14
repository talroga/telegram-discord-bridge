"""Discord handler."""
import os
import uuid
import asyncio
import sys
from typing import List, Sequence
import telethon.types as tl
from telethon.tl.functions.photos import GetUserPhotosRequest
from telethon import TelegramClient

import discord
from discord import Message, MessageReference, TextChannel

from bridge.config import Config
from bridge.history import MessageHistoryHandler
from bridge.logger import Logger
from bridge.utils import split_message

# from discord.abc import GuildChannel, PrivateChannel,

logger = Logger.get_logger(Config().app.name)
history_manager = MessageHistoryHandler()


async def start_discord(config: Config) -> discord.Client:
    """Start the Discord client."""
    async def start_discord_client(discord_client: discord.Client, token: str):
        try:
            logger.info("Starting Discord client...")

            # setup discord logger
            discord_logging_handler = Logger.generate_handler(
                f"{config.app.name}_discord", config.logger)
            discord.utils.setup_logging(handler=discord_logging_handler)

            await discord_client.start(token)
            logger.info("Discord client started the session: %s, with identity: %s",
                        config.app.name, discord_client.user.id)

        except (discord.LoginFailure, TypeError) as login_failure:
            logger.error(
                "Error while connecting to Discord: %s", login_failure)
            sys.exit(1)
        except discord.HTTPException as http_exception:
            logger.critical(
                "Discord client failed to connect with status: %s - %s", http_exception.status, http_exception.response.reason)

    discord_client = discord.Client(intents=discord.Intents.default())
    _ = asyncio.ensure_future(
        start_discord_client(discord_client, config.discord.bot_token))

    return discord_client

#  -> Optional[Union[GuildChannel, Thread, PrivateChannel]]:

async def forward_embed_to_discord(telegram_client: TelegramClient, discord_channel: TextChannel, event) -> List[Message]:
    sent_messages = []
    files = []
    message_parts = split_message(event.message.message)
    embed = discord.Embed(type="rich", colour=discord.Color.teal())
    try:
        if event.message.forward:
            timestamp = event.message.forward.date
            thumbnail_path = await telegram_client.download_profile_photo(event.message.forward.chat, file=str(uuid.uuid1()))
            author_path = await telegram_client.download_profile_photo(event.chat,file=str(uuid.uuid1()))
            main_url="https://t.me/" + event.message.forward.chat.username + "/" + str(event.message.forward.channel_post)
            author_url="https://t.me/" + event.chat.username + "/" + str(event._message_id)
            main_title=event.message.forward.chat.title
            author_title=event.chat.title

            embed.url=main_url
            embed.timestamp=timestamp
            embed.title=main_title

            author_file=discord.File(str(author_path))
            thumbnail_file=discord.File(str(thumbnail_path))
            files.append(author_file)
            files.append(thumbnail_file)
            embed.set_author(name=author_title,url=author_url,icon_url="attachment://"+str(author_path))
            embed.set_thumbnail(url="attachment://"+str(thumbnail_path))

        else:
            timestamp = event.message.date
            main_url="https://t.me/" + event.chat.username + "/" + str(event._message_id)
            main_title=event.chat.title
            author_path = await telegram_client.download_profile_photo(event.chat,file=str(uuid.uuid1()))

            embed.timestamp=timestamp
            embed.url = main_url
            embed.title=main_title

            author_file=discord.File(str(author_path))
            files.append(author_file)
            embed.set_thumbnail(url="attachment://"+str(author_path))
        if event.message.media:
            media_path = await telegram_client.download_media(event.message,file=str(uuid.uuid1()))
            discord_file = discord.File(str(media_path))
            files.append(discord_file)
            if event.message.media.document.mime_type.split('/')[0] == 'image':
                embed.set_image(url="attachment://"+str(media_path))

        for message_part in message_parts:
                embed.description=message_part
                sent_message = await discord_channel.send(embed=embed, files=files)
                sent_messages.append(sent_message)
    except Exception as ex:
        logger.error("An error occured while sending a message to discord.")
        return sent_messages
    finally:
        for file in files:
            os.remove(file._filename)
    return sent_messages

async def forward_to_discord(discord_channel: TextChannel, message_text: str,
                             image_file=None, reference: MessageReference = ...) -> List[Message]:
    """Send a message to Discord."""
    sent_messages = []
    message_parts = split_message(message_text)
    try:
        if image_file:
            discord_file = discord.File(image_file)
            sent_message = await discord_channel.send(message_parts[0],
                                                      file=discord_file,
                                                      reference=reference)
            sent_messages.append(sent_message)
            message_parts.pop(0)

        for part in message_parts:
            sent_message = await discord_channel.send(part, reference=reference)
            sent_messages.append(sent_message)
    except discord.Forbidden:
        logger.error("Discord client doesn't have permission to send messages to channel %s",
                     discord_channel.id, exc_info=Config().app.debug)
    except discord.HTTPException as http_exception:
        logger.error("Error while sending message to Discord: %s",
                     http_exception, exc_info=Config().app.debug)

    return sent_messages


async def fetch_discord_reference(event, forwarder_name: str, discord_channel) -> MessageReference | None:
    """Fetch the Discord message reference."""
    discord_message_id = await history_manager.get_discord_message_id(
        forwarder_name,
        event.message.reply_to_msg_id)
    if not discord_message_id:
        logger.debug("No mapping found for TG message %s",
                     event.message.reply_to_msg_id)
        return None

    try:
        messages = []
        async for message in discord_channel.history(around=discord.Object(id=discord_message_id),   # pylint: disable=line-too-long
                                                     limit=10):
            messages.append(message)

        discord_message = next(
            (msg for msg in messages if msg.id == discord_message_id), None)
        if not discord_message:
            logger.debug(
                "Reference Discord message not found for TG message %s",
                event.message.reply_to_msg_id)
            return None

        return MessageReference.from_message(discord_message)
    except discord.NotFound:
        logger.debug("Reference Discord message not found for TG message %s",
                     event.message.reply_to_msg_id)
        return None


def get_mention_roles(message_forward_hashtags: List[str],
                      mention_override: dict,
                      discord_built_in_roles: List[str],
                      server_roles: Sequence[discord.Role]) -> List[str]:
    """Get the roles to mention."""
    mention_roles = set()

    for tag in message_forward_hashtags:
        if tag.lower() in mention_override:
            logger.debug("Found mention override for tag %s: %s",
                         tag, mention_override[tag.lower()])
            for role_name in mention_override[tag.lower()]:
                if is_builtin_mention(role_name, discord_built_in_roles):
                    mention_roles.add("@" + role_name)
                else:
                    role = discord.utils.get(server_roles, name=role_name)
                    if role:
                        mention_roles.add(role.mention)

    return list(mention_roles)


def is_builtin_mention(role_name: str, discord_built_in_roles: List[str]) -> bool:
    """Check if a role name is a Discord built-in mention."""
    return role_name.lower() in discord_built_in_roles
