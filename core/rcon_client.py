"""Asynchronous RCON client for L4D2 server communication.

Implements the Source RCON protocol on top of the standard library and wraps
it in an async interface with automatic retry and connection timeout handling,
so no external RCON library is needed.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("astrbot")

# RCON connection timeout in seconds
RCON_TIMEOUT = 10
# Maximum number of retry attempts for failed RCON commands
MAX_RETRIES = 3
# Delay between retries in seconds
RETRY_DELAY = 1
# Distance between a request id and the id of its end-of-response sentinel
SENTINEL_ID_OFFSET = 100000
# Silence that ends a response on servers which do not answer the sentinel
IDLE_TIMEOUT = 1.5
# Smallest slice of the budget handed to a single socket operation
MIN_OPERATION_TIMEOUT = 1.0

# Source RCON packet types
SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_AUTH = 3


class RconError(Exception):
    """Raised when an RCON operation fails after all retry attempts."""


class _RconTimeout(RconError):
    """Raised when no RCON packet arrives within the idle timeout."""


class _RconConnection:
    """Synchronous Source RCON connection over a TCP socket.

    Wire format of every packet: ``int32 size | int32 request id | int32 type |
    body | 0x00 0x00``, where ``size`` counts the bytes after itself. One
    connection serves exactly one command and is then closed, which keeps the
    protocol state (authentication, request ids) trivial.

    Attributes:
        host: Server IP address.
        port: RCON port number.
        password: RCON password used for authentication.
        timeout: Socket timeout in seconds.
        connect: Optional factory that receives the remaining timeout in
            seconds and returns a connected socket-like object. It is used to
            reach the server through an SSH tunnel; when omitted a direct TCP
            connection is opened.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        timeout: int,
        connect: Callable[[float], object] | None = None,
    ) -> None:
        """Initialize the connection.

        Args:
            host: Server IP address.
            port: RCON port number.
            password: RCON password used for authentication.
            timeout: Socket timeout in seconds.
            connect: Optional factory that receives the remaining timeout in
                seconds and returns a connected socket-like object, used for
                tunnelled connections.
        """
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._connect = connect
        self._sock: Any = None
        self._request_id = 1
        self._deadline = 0.0

    def _remaining(self) -> float:
        """Return the time left for the whole connection attempt.

        Returns:
            Seconds left before the overall timeout expires, never below
            ``MIN_OPERATION_TIMEOUT`` so that a socket is never put into
            non-blocking mode by accident.
        """
        return max(self._deadline - time.monotonic(), MIN_OPERATION_TIMEOUT)

    @staticmethod
    def _encode(request_id: int, packet_type: int, body: str) -> bytes:
        """Serialize one RCON packet.

        Args:
            request_id: Request id the server echoes back in its reply.
            packet_type: One of the ``SERVERDATA_*`` constants.
            body: Packet body, usually the console command.

        Returns:
            The packet as bytes, ready to be sent.
        """
        payload = (
            struct.pack("<ii", request_id, packet_type)
            + body.encode("utf-8")
            + b"\x00\x00"
        )
        return struct.pack("<i", len(payload)) + payload

    def _read_exactly(self, count: int) -> bytes:
        """Read exactly ``count`` bytes from the socket.

        Args:
            count: Number of bytes to read.

        Returns:
            The bytes read.

        Raises:
            _RconTimeout: If no data arrives within the socket timeout.
            RconError: If the connection drops or is closed.
        """
        data = b""
        while len(data) < count:
            try:
                chunk = self._sock.recv(count - len(data))
            except TimeoutError as e:
                current = self._sock.gettimeout()
                raise _RconTimeout(
                    f"RCON 响应超时（等待 {current:.1f}s 无数据）"
                ) from e
            except OSError as e:
                raise RconError(f"RCON 连接中断：{e}") from e
            if not chunk:
                raise RconError("RCON 连接已被服务端关闭")
            data += chunk
        return data

    def _read_packet(self) -> tuple[int, str]:
        """Read one RCON packet.

        Returns:
            A tuple of (request_id, body).

        Raises:
            RconError: If the packet is malformed or the connection drops.
        """
        (size,) = struct.unpack("<i", self._read_exactly(4))
        if size < 10:
            raise RconError(f"RCON 数据包格式异常（size={size}）")
        payload = self._read_exactly(size)
        request_id, _packet_type = struct.unpack("<ii", payload[:8])
        return request_id, payload[8:-2].decode("utf-8", errors="replace")

    def _send(self, request_id: int, packet_type: int, body: str) -> None:
        """Send one RCON packet.

        Args:
            request_id: Request id for this packet.
            packet_type: One of the ``SERVERDATA_*`` constants.
            body: Packet body, usually the console command.

        Raises:
            RconError: If the packet cannot be sent.
        """
        try:
            self._sock.sendall(self._encode(request_id, packet_type, body))
        except OSError as e:
            raise RconError(f"RCON 指令发送失败：{e}") from e

    def _authenticate(self) -> None:
        """Authenticate against the server.

        Raises:
            RconError: If the password is rejected.
        """
        self._sock.settimeout(self._remaining())
        self._send(self._request_id, SERVERDATA_AUTH, self.password)
        request_id, _ = self._read_packet()
        # A rejected password is answered with the reserved id -1.
        if request_id == -1:
            raise RconError("RCON 认证失败：密码错误，请检查 rcon_password 配置")

    def __enter__(self) -> _RconConnection:
        """Connect and authenticate.

        Returns:
            This connection, ready for :meth:`command`.

        Raises:
            RconError: If the connection or authentication fails.
        """
        # Connecting, authenticating and reading the reply all share one budget,
        # and every step only gets whatever is left of it, so a failure reports
        # the step that actually failed instead of a generic overall timeout.
        self._deadline = time.monotonic() + self.timeout
        try:
            if self._connect is not None:
                self._sock = self._connect(self._remaining())
            else:
                self._sock = socket.create_connection(
                    (self.host, self.port), self._remaining()
                )
        except Exception as e:
            via = "（经 SSH 通道）" if self._connect is not None else ""
            raise RconError(
                f"无法连接 RCON {self.host}:{self.port}{via}：{e}\n"
                f"排查建议：\n"
                f"  1. server.cfg 必须设置 rcon_password，且改动后要重启服务端"
                f"（Source 引擎只有设置了它才会监听 RCON 端口）；\n"
                f"  2. RCON 与游戏使用同一个端口号，但 RCON 走 TCP、游戏走 UDP——"
                f"在服务器上执行 ss -lntp | grep {self.port} 确认该端口有监听"
                f"（插件会自动改用服务端实际监听的地址，含 -ip 指定的内网地址）；\n"
                f"  3. 服务端若在 Docker 等容器里，需要映射 TCP 端口"
                f"（-p {self.port}:{self.port}/tcp），"
                f"或把插件配置里的「RCON 隧道目标地址」填成容器 IP。"
            ) from e
        self._sock.settimeout(self._remaining())
        self._authenticate()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        """Close the socket if it is still open."""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def command(self, command: str) -> str:
        """Run a console command and collect its complete response.

        Args:
            command: Console command to execute.

        Returns:
            The response text, with all response packets concatenated.

        Raises:
            RconError: If the response cannot be read completely.
        """
        self._request_id += 1
        request_id = self._request_id
        sentinel_id = request_id + SENTINEL_ID_OFFSET
        self._send(request_id, SERVERDATA_EXECCOMMAND, command)
        # srcds has no end-of-response marker and splits long replies over
        # several packets. An extra empty request is answered after all of
        # them, so its reply marks the end of the output.
        self._send(sentinel_id, SERVERDATA_RESPONSE_VALUE, "")

        parts: list[str] = []
        while True:
            # While nothing has arrived yet the whole remaining budget may be
            # used; once chunks are flowing, a short silence means the response
            # has ended (some engines never answer the sentinel request).
            self._sock.settimeout(
                min(IDLE_TIMEOUT, self._remaining()) if parts else self._remaining()
            )
            try:
                packet_id, body = self._read_packet()
            except _RconTimeout:
                # Engines that ignore the sentinel request end their response
                # with silence instead, so keep what was received so far.
                if parts:
                    return "".join(parts)
                raise
            if packet_id == sentinel_id:
                return "".join(parts)
            if packet_id == request_id:
                parts.append(body)
                # Response chunks are sent back to back, so from here on a
                # short silence means the server has nothing more to say.
                self._sock.settimeout(IDLE_TIMEOUT)
            # Packets with any other id (for example the empty one srcds sends
            # right after authentication) are stale and skipped.


class RconClient:
    """Asynchronous RCON client for communicating with a Source engine server.

    Each execute call opens a fresh connection, so a dropped or stale connection
    can never break later commands. A per-client async lock prevents concurrent
    command interleaving.

    Attributes:
        host: Server IP address.
        port: RCON port number.
        password: RCON password.
        timeout: Connection and command timeout in seconds.
        max_retries: Maximum retry attempts per command.
        connect: Optional factory that receives the remaining timeout in
            seconds and returns a connected socket-like object, used to reach
            the server through an SSH tunnel.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        timeout: int = RCON_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        connect: Callable[[float], object] | None = None,
    ) -> None:
        """Initialize the RCON client.

        Args:
            host: Server IP address.
            port: RCON port number.
            password: RCON password.
            timeout: Connection and command timeout in seconds.
            max_retries: Maximum retry attempts per command.
            connect: Optional factory that receives the remaining timeout in
                seconds and returns a connected socket-like object, for example
                an SSH tunnel to a port that is not exposed.
        """
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.max_retries = max_retries
        self._connect = connect
        self._lock = asyncio.Lock()
        self._offline = False

    @property
    def is_offline(self) -> bool:
        """Whether the server has been marked offline after repeated failures."""
        return self._offline

    def mark_online(self) -> None:
        """Reset the offline flag, e.g. after a successful process start."""
        self._offline = False

    async def execute(self, command: str, attempts: int | None = None) -> str:
        """Execute an RCON command asynchronously with retry logic.

        Args:
            command: The RCON command string to execute.
            attempts: Number of attempts for this command. Defaults to
                ``max_retries``. Commands that are not safe to repeat (for
                example ``say`` or ``kick``) pass ``1`` so a lost reply cannot
                make the action happen twice.

        Returns:
            The raw text response from the server.

        Raises:
            RconError: If the command fails after all retry attempts.
        """
        max_attempts = attempts if attempts is not None else self.max_retries
        async with self._lock:
            last_error: Exception | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    loop = asyncio.get_running_loop()
                    # The worker enforces the command timeout itself, so leave it
                    # room to finish and report the real reason for a failure.
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, self._execute_sync, command),
                        timeout=self.timeout + 5,
                    )
                    self._offline = False
                    return result
                except asyncio.TimeoutError:
                    last_error = TimeoutError(f"RCON 指令超时（{self.timeout}s）")
                    logger.warning(
                        "RCON timeout (attempt %d/%d) for %s:%d: %s",
                        attempt,
                        max_attempts,
                        self.host,
                        self.port,
                        command,
                    )
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "RCON error (attempt %d/%d) for %s:%d: %s",
                        attempt,
                        max_attempts,
                        self.host,
                        self.port,
                        e,
                    )

                if attempt < max_attempts:
                    await asyncio.sleep(RETRY_DELAY)

            self._offline = True
            raise RconError(
                f"RCON 指令失败（已尝试 {max_attempts} 次）：{last_error}"
            ) from last_error

    def _execute_sync(self, command: str) -> str:
        """Execute an RCON command synchronously.

        Runs in a thread executor to avoid blocking the event loop.

        Args:
            command: The RCON command string.

        Returns:
            The raw text response from the server.

        Raises:
            RconError: If the connection or the command fails.
        """
        with _RconConnection(
            self.host, self.port, self.password, self.timeout, self._connect
        ) as connection:
            return connection.command(command)

    def _quit_sync(self) -> None:
        """Send the 'quit' command and consume its reply.

        The reply is only read to drain the socket: closing a connection that
        still holds unread data resets it, and the resulting RST can make the
        server drop the shutdown request. A reply that never arrives is normal
        for a server that is already going down.
        """
        with _RconConnection(
            self.host, self.port, self.password, self.timeout, self._connect
        ) as connection:
            try:
                connection.command("quit")
            except RconError as e:
                logger.debug(
                    "RCON quit for %s:%d had no complete reply (expected while "
                    "the server shuts down): %s",
                    self.host,
                    self.port,
                    e,
                )

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
        # A single attempt only: repeating a broadcast would spam the server.
        return await self.execute(f'say "{message}"', attempts=1)

    async def kick(self, player_name: str) -> str:
        """Kick a player from the server by name.

        Args:
            player_name: The display name of the player to kick.

        Returns:
            RCON response.
        """
        # A single attempt only: the player is already gone after the first
        # kick, so retrying would report a failure for a successful kick.
        return await self.execute(f'kick "{player_name}"', attempts=1)

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

        The reply is not awaited, because a quitting server drops the connection
        while it shuts down. Failures are logged and swallowed so that callers
        can fall back to terminating the process instead of reporting an error.

        Returns:
            An empty string.
        """
        try:
            loop = asyncio.get_running_loop()
            # The worker enforces its own socket timeout, so give it room to
            # finish and report why it failed instead of racing it.
            await asyncio.wait_for(
                loop.run_in_executor(None, self._quit_sync),
                timeout=self.timeout + 5,
            )
        except Exception as e:
            logger.info(
                "RCON quit for %s:%d could not be delivered: %s: %s",
                self.host,
                self.port,
                type(e).__name__,
                e,
            )
        return ""
