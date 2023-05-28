"""handles the process of the bridge between telegram and discord"""

import argparse
import asyncio
import os
import signal
import sys
from asyncio import AbstractEventLoop
from typing import Tuple

import discord
import psutil  # pylint: disable=import-error
from telethon import TelegramClient

from bridge.config import Config
from bridge.core import on_restored_connectivity, start
from bridge.discord_handler import start_discord
from bridge.enums import ProcessStateEnum
from bridge.healtcheck_handler import healthcheck
from bridge.logger import Logger
from bridge.telegram_handler import start_telegram_client

config = Config()
logger = Logger.init_logger(config.app.name, config.logger)


def create_pid_file() -> str:
    """Create a PID file."""

    # Get the process ID.
    pid = os.getpid()

    # Create the PID file.
    bot_pid_file = f'{config.app.name}.pid'
    process_state, _ = determine_process_state(bot_pid_file)

    if process_state == "running":
        sys.exit(1)

    try:
        with open(bot_pid_file, "w", encoding="utf-8") as pid_file:
            pid_file.write(str(pid))
    except OSError as err:
        sys.exit(f"Unable to create PID file: {err}")

    return bot_pid_file


def remove_pid_file(pid_file: str):
    """Remove a PID file."""
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        logger.error("PID file '%s' not found.", pid_file)
    except Exception as ex:  # pylint: disable=broad-except
        logger.exception(ex)
        logger.error("Failed to remove PID file '%s'.", pid_file)


def determine_process_state(pid_file: str) -> Tuple[str, int]:
    """
    Determine the state of the process.

    The state of the process is determined by looking for the PID file. If the
    PID file does not exist, the process is considered stopped. If the PID file
    does exist, the process is considered running.

    If the PID file exists and the PID of the process that created it is not
    running, the process is considered stopped. If the PID file exists and the
    PID of the process that created it is running, the process is considered
    running.

    :param pid_file: The path to the PID file.
    :type pid_file: str
    :return: A tuple containing the process state and the PID of the process
    that created the PID file.
    :rtype: Tuple[str, int]
    """

    if not os.path.isfile(pid_file):
        # The PID file does not exist, so the process is considered stopped.
        return ProcessStateEnum.STOPPED, 0

    pid = 0
    try:
        # Read the PID from the PID file.
        with open(pid_file, "r", encoding="utf-8") as bot_pid_file:
            pid = int(bot_pid_file.read().strip())

            # If the PID file exists and the PID of the process that created it
            # is not running, the process is considered stopped.
            if not psutil.pid_exists(pid):
                return ProcessStateEnum.STOPPED, 0

            # If the PID file exists and the PID of the process that created it
            # is running, the process is considered running.
            return ProcessStateEnum.RUNNING, pid
    except ProcessLookupError:
        # If the PID file exists and the PID of the process that created it is
        # not running, the process is considered stopped.
        return ProcessStateEnum.ORPHANED, 0
    except PermissionError:
        # If the PID file exists and the PID of the process that created it is
        # running, the process is considered running.
        return ProcessStateEnum.RUNNING, pid
    except FileNotFoundError:
        # The PID file does not exist, so the process is considered stopped.
        return ProcessStateEnum.STOPPED, 0


def stop_bridge():
    """Stop the bridge."""
    pid_file = f'{config.app.name}.pid'

    process_state, pid = determine_process_state(pid_file)
    if process_state == ProcessStateEnum.STOPPED:
        logger.warning(
            "PID file '%s' not found. The %s may not be running.", pid_file, config.app.name)
        return

    try:
        os.kill(pid, signal.SIGINT)
        logger.warning("Sent SIGINT to the %s process with PID %s.",
                       config.app.name, pid)
    except ProcessLookupError:
        logger.error(
            "The %s process with PID %s is not running.", config.app.name, pid)


async def on_shutdown(telegram_client, discord_client):
    """Shutdown the bridge."""
    logger.info("Starting shutdown process...")
    task = asyncio.current_task()
    all_tasks = asyncio.all_tasks()

    try:
        logger.info("Disconnecting Telegram client...")
        await telegram_client.disconnect()
        logger.info("Telegram client disconnected.")
    except (Exception, asyncio.CancelledError) as ex:  # pylint: disable=broad-except
        logger.error("Error disconnecting Telegram client: %s", {ex})

    try:
        logger.info("Disconnecting Discord client...")
        await discord_client.close()
        logger.info("Discord client disconnected.")
    except (Exception, asyncio.CancelledError) as ex:  # pylint: disable=broad-except
        logger.error("Error disconnecting Discord client: %s", {ex})

    for running_task in all_tasks:
        if running_task is not task:
            if task is not None:
                logger.debug("Cancelling task %s...", {running_task})
                task.cancel()

    logger.debug("Stopping event loop...")
    asyncio.get_event_loop().stop()
    logger.info("Shutdown process completed.")


async def shutdown(sig, tasks_loop: asyncio.AbstractEventLoop):
    """Shutdown the application gracefully."""
    logger.warning("shutdown received signal %s, shutting down...", {sig})

    # Cancel all tasks
    tasks = [task for task in asyncio.all_tasks(
    ) if task is not asyncio.current_task()]

    for task in tasks:
        task.cancel()

    # Wait for all tasks to be cancelled
    results = await asyncio.gather(*tasks, return_exceptions=config.app.debug)

    # Check for errors
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            continue
        if isinstance(result, Exception):
            logger.error("Error during shutdown: %s", result)

    # Stop the loop
    if tasks_loop is not None:
        tasks_loop.stop()


async def handle_signal(sig, tgc: TelegramClient, dcl: discord.Client, tasks):
    """Handle graceful shutdown on received signal."""
    logger.warning("Received signal %s, shutting down...", {sig})

    # Disconnect clients
    if tgc.is_connected():
        tgc.disconnect()
    if dcl.is_ready():
        await dcl.close()

    # Cancel all tasks
    await asyncio.gather(*tasks, return_exceptions=config.app.debug)


async def init_clients() -> Tuple[TelegramClient, discord.Client]:
    """Handle the initialization of the bridge's clients."""
    telegram_client_instance = await start_telegram_client(config)
    discord_client_instance = await start_discord(config)

    event_loop = asyncio.get_event_loop()

    # Set signal handlers for graceful shutdown on received signal (except on Windows)
    # NOTE: This is not supported on Windows
    if os.name != 'nt':
        for sig in (signal.SIGINT, signal.SIGTERM):
            event_loop.add_signal_handler(
                sig, lambda sig=sig: asyncio.create_task(shutdown(sig, tasks_loop=event_loop)))  # type: ignore

    try:
        # Create tasks for starting the main logic and waiting for clients to disconnect
        start_task = asyncio.create_task(
            start(telegram_client_instance, discord_client_instance, config)
        )
        telegram_wait_task = asyncio.create_task(
            telegram_client_instance.run_until_disconnected()  # type: ignore
        )
        discord_wait_task = asyncio.create_task(
            discord_client_instance.wait_until_ready()
        )
        api_healthcheck_task = event_loop.create_task(
            healthcheck(telegram_client_instance,
                        discord_client_instance, config.app.healthcheck_interval)
        )
        on_restored_connectivity_task = event_loop.create_task(
            on_restored_connectivity(
                config=config,
                telegram_client=telegram_client_instance,
                discord_client=discord_client_instance)
        )

        await asyncio.gather(start_task,
                             telegram_wait_task,
                             discord_wait_task,
                             api_healthcheck_task,
                             on_restored_connectivity_task, return_exceptions=config.app.debug)

    except asyncio.CancelledError as ex:
        logger.warning(
            "on_restored_connectivity_task CancelledError caught: %s", ex, exc_info=False)
    except Exception as ex:  # pylint: disable=broad-except
        logger.error("Error while running the bridge: %s",
                     ex, exc_info=config.app.debug)
    finally:
        await on_shutdown(telegram_client_instance, discord_client_instance)

    return telegram_client_instance, discord_client_instance


def start_bridge(event_loop: AbstractEventLoop):
    """Start the bridge."""

    # Set the exception handler.
    event_loop.set_exception_handler(event_loop_exception_handler)

    # Create a PID file.
    pid_file = create_pid_file()

    # Create a task for the main coroutine.
    main_task = event_loop.create_task(main())

    try:
        # Run the event loop.
        event_loop.run_forever()
    except KeyboardInterrupt:
        # Cancel the main task.
        main_task.cancel()
    except asyncio.CancelledError:
        pass
    except asyncio.LimitOverrunError as ex:
        logger.error(
            "The event loop has exceeded the configured limit of pending tasks: %s",
            ex, exc_info=config.app.debug)
    except Exception as ex:  # pylint: disable=broad-except
        logger.error("Error while running the bridge: %s",
                     ex, exc_info=config.app.debug)
    finally:
        # Remove the PID file.
        remove_pid_file(pid_file)


def event_loop_exception_handler(event_loop: AbstractEventLoop, context):
    """Asyncio Event loop exception handler."""
    try:
        exception = context.get("exception")
        if not isinstance(exception, asyncio.CancelledError):
            event_loop.default_exception_handler(context)
        else:
            # This error is expected during shutdown.
            logger.warning("CancelledError caught during shutdown")
    except Exception as ex:  # pylint: disable=broad-except
        logger.error(
            "Event loop exception handler failed: %s",
            ex,
            exc_info=True,
        )


def daemonize_process():
    """Daemonize the process by forking and redirecting standard file descriptors."""
    # Fork the process and exit if we're the parent
    pid = os.fork()
    if pid > 0:
        sys.exit()

    # Start a new session
    os.setsid()

    # Fork again and exit if we're the parent
    pid = os.fork()
    if pid > 0:
        sys.exit()

    # Redirect standard file descriptors to /dev/null
    with open(os.devnull, "r", encoding="utf-8") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        os.dup2(devnull.fileno(), sys.stdout.fileno())
        os.dup2(devnull.fileno(), sys.stderr.fileno())


async def main():
    """Run the bridge."""
    clients = ()
    try:
        clients = await init_clients()
    except KeyboardInterrupt:
        logger.warning("Interrupted by user, shutting down...")
    except asyncio.CancelledError:
        logger.warning("CancelledError caught, shutting down...")
    finally:
        if clients:
            telegram_client, discord_client = clients[0], clients[1]
            if not telegram_client.is_connected() and not discord_client.is_ready():
                clients = ()
            else:
                await on_shutdown(telegram_client, discord_client)
                clients = ()


def controller(boot: bool, stop: bool, background: bool):
    """Init the bridge."""
    if boot:
        logger.info("Booting %s...", config.app.name)
        logger.info("Version: %s", config.app.version)
        logger.info("Description: %s", config.app.description)
        logger.info("Log level: %s", config.logger.level)
        logger.info("Debug enabled: %s", config.app.debug)
        logger.info("Login through API enabled: %s",
                    config.api.telegram_login_enabled)

        if background:
            logger.info("Running %s in the background", config.app.name)
            if os.name != "posix":
                logger.error(
                    "Background mode is only supported on POSIX systems")
                sys.exit(1)

            if config.logger.console is True:
                logger.error(
                    "Background mode requires console logging to be disabled")
                sys.exit(1)

            logger.info("Starting %s in the background...", config.app.name)
            daemonize_process()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        start_bridge(loop)
    elif stop:
        stop_bridge()
    else:
        print("Please use --start or --stop flags to start or stop the bridge.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Process handler for the bridge.")
    parser.add_argument("--start", action="store_true",
                        help="Start the bridge.")

    parser.add_argument("--stop", action="store_true", help="Stop the bridge.")

    parser.add_argument("--background", action="store_true",
                        help="Run the bridge in the background (forked).")

    parser.add_argument("--version", action="store_true",
                        help="Get the Bridge version.")

    cmd_args = parser.parse_args()

    if cmd_args.version:
        print(f'The Bridge\nv{config.app.version}')
        sys.exit(0)

    __start: bool = cmd_args.start
    __stop: bool = cmd_args.stop
    __background: bool = cmd_args.background

    controller(__start, __stop, __background)
