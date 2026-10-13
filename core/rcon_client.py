"""Asynchronous RCON client wrapper for L4D2 server communication.

Wraps the synchronous python-valve RCON library in an async interface
with automatic retry and connection timeout handling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("astrbot")

# RCON connection timeout in seconds
RCON_TIMEOUT = 10
# Maximum number of retry attempts for failed RCON commands
MAX_RETRIES = 3
# Delay between retries in seconds
RETRY_DELAY = 1


class RconError(Exception):
    """Raised when an RCON operation fails after all retry attempts."""


class RconClient:
    """Asynchronous RCON client for communicating with a Source engine server.

    Each execute call opens a fresh RCON connection (python-valve does not
    support persistent connections safely across event loop iterations).
    A per-client async lock prevents concurrent command interleaving.

    Attributes:
        host: Server IP address.
        port: RCON port number.
        password: RCON password.
        timeout: Connection and command timeout in seconds.
        max_retries: Maximum retry attempts per command.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        timeout: int = RCON_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        """Initialize the RCON client.

        Args:
            host: Server IP address.
            port: RCON port number.
            password: RCON password.
            timeout: Connection and command timeout in seconds.
            max_retries: Maximum retry attempts per command.
        """
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.max_retries = max_retries
        self._lock = asyncio.Lock()
        self._offline = False

    @property
    def is_offline(self) -> bool:
        """Whether the server has been marked offline after repeated failures."""
        return self._offline

    def mark_online(self) -> None:
        """Reset the offline flag, e.g. after a successful process start."""
        self._offline = False

    async def execute(self, command: str) -> str:
        """Execute an RCON command asynchronously with retry logic.

        Args:
            command: The RCON command string to execute.

        Returns:
            The raw text response from the server.

        Raises:
            RconError: If the command fails after all retry attempts.
        """
        async with self._lock:
            last_error: Optional[Exception] = None

            for attempt in range(1, self.max_retries + 1):
                try:
                    loop = asyncio.get_running_loop()
                    result = await asyncio.wait_for(
                        loop.run_in_executor(
                            None, self._execute_sync, command
                        ),
                        timeout=self.timeout,
                    )
                    self._offline = False
                    return result
                except asyncio.TimeoutError:
                    last_error = TimeoutError(
                        f"RCON command timed out after {self.timeout}s"
                    )
                    logger.warning(
                        "RCON timeout (attempt %d/%d) for %s:%d: %s",
                        attempt,
                        self.max_retries,
                        self.host,
                        self.port,
                        command,
                    )
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "RCON error (attempt %d/%d) for %s:%d: %s",
                        attempt,
                        self.max_retries,
                        self.host,
                        self.port,
                        e,
                    )

                if attempt < self.max_retries:
                    await asyncio.sleep(RETRY_DELAY)

            self._offline = True
            raise RconError(
                f"RCON command failed after {self.max_retries} attempts: {last_error}"
            ) from last_error

    def _execute_sync(self, command: str) -> str:
        """Execute an RCON command synchronously using python-valve.

        Runs in a thread executor to avoid blocking the event loop.

        Args:
            command: The RCON command string.

        Returns:
            The raw text response from the server.

        Raises:
            ImportError: If python-valve is not installed.
            Exception: If the RCON connection or command fails.
        """
        try:
            from valve.source.rcon import RCON  # type: ignore
        except ImportError as e:
            raise ImportError(
                "python-valve is not installed. Run: pip install python-valve"
            ) from e

        with RCON((self.host, self.port), self.password) as rcon:
            return rcon(command)

    async def status(self) -> str:
        """Execute the 'status' command and return raw output.

        Returns:
            Raw status output from the server.
        """
        return await self.execute("status")

    async def say(self, message: str) -> str:
        """Send a global chat message to the server.

        Args:
            message: The message text to broadcast.

        Returns:
            RCON response (usually empty for 'say').
        """
        return await self.execute(f'say "{message}"')

    async def kick(self, player_name: str) -> str:
        """Kick a player from the server by name.

        Args:
            player_name: The display name of the player to kick.

        Returns:
            RCON response.
        """
        return await self.execute(f'kick "{player_name}"')

    async def changelevel(self, map_name: str) -> str:
        """Change the current map.

        Args:
            map_name: The map name to change to (e.g. c2m1_highway).

        Returns:
            RCON response.
        """
        return await self.execute(f"changelevel {map_name}")

    async def set_difficulty(self, difficulty: str) -> str:
        """Set the game difficulty.

        Args:
            difficulty: One of 'easy', 'normal', 'hard', 'expert'.

        Returns:
            RCON response.
        """
        return await self.execute(f"z_difficulty {difficulty}")

    async def restart_game(self) -> str:
        """Restart the current game round.

        Returns:
            RCON response.
        """
        return await self.execute("mp_restartgame 1")

    async def quit(self) -> str:
        """Send the 'quit' command to gracefully shut down the server.

        Returns:
            RCON response (usually empty before disconnect).
        """
        return await self.execute("quit")
