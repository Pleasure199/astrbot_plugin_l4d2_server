"""L4D2 dedicated server process manager.

Handles starting, stopping, restarting, and monitoring srcds processes
across Windows and Linux platforms.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Optional

from .models import ServerConfig
from .rcon_client import RconClient
from .utils import is_linux, is_windows

logger = logging.getLogger("astrbot")

# Time to wait for graceful shutdown via RCON before force-killing (seconds)
GRACEFUL_SHUTDOWN_TIMEOUT = 15
# Time to wait for process to terminate after SIGTERM (seconds)
TERMINATE_TIMEOUT = 10


class ProcessManager:
    """Manages L4D2 dedicated server (srcds) processes.

    Tracks running processes by server name and provides cross-platform
    start/stop/restart operations. A per-server async lock prevents
    concurrent state changes on the same server.

    Attributes:
        servers: Dict mapping server name to ServerConfig.
    """

    def __init__(self, servers: list[ServerConfig]) -> None:
        """Initialize the process manager.

        Args:
            servers: List of ServerConfig objects to manage.
        """
        self.servers: dict[str, ServerConfig] = {s.name: s for s in servers}
        self._processes: dict[str, subprocess.Popen] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, server_name: str) -> asyncio.Lock:
        """Get or create an async lock for the given server.

        Args:
            server_name: Name of the server.

        Returns:
            An asyncio.Lock instance for the server.
        """
        if server_name not in self._locks:
            self._locks[server_name] = asyncio.Lock()
        return self._locks[server_name]

    def get_server(self, server_name: str) -> Optional[ServerConfig]:
        """Get the configuration for a named server.

        Args:
            server_name: Name of the server.

        Returns:
            ServerConfig if found, None otherwise.
        """
        return self.servers.get(server_name)

    def get_all_server_names(self) -> list[str]:
        """Return a list of all configured server names.

        Returns:
            List of server names.
        """
        return list(self.servers.keys())

    def is_running(self, server_name: str) -> bool:
        """Check if a server process is currently running.

        Args:
            server_name: Name of the server.

        Returns:
            True if the process is running, False otherwise.
        """
        proc = self._processes.get(server_name)
        if proc is None:
            return False
        return proc.poll() is None

    def get_running_servers(self) -> set[str]:
        """Return the set of server names that are currently running.

        Returns:
            Set of running server names.
        """
        return {
            name
            for name in self._processes
            if self._processes[name].poll() is None
        }

    def _build_start_command(self, config: ServerConfig) -> list[str]:
        """Build the command-line arguments for starting srcds.

        Args:
            config: Server configuration.

        Returns:
            List of command-line arguments.

        Raises:
            ValueError: If srcds_path is not configured.
        """
        if not config.srcds_path:
            raise ValueError(
                f"服务器 '{config.name}' 未配置 srcds_path，无法启动。"
            )

        srcds = Path(config.srcds_path)
        base_args = [
            str(srcds),
            "-game",
            "left4dead2",
            "-port",
            str(config.port),
            "+hostport",
            str(config.port),
            "-maxplayers",
            str(config.max_players),
            "+map",
            config.default_map,
            "-console",
        ]

        # Append user-defined start parameters
        if config.start_params:
            base_args.extend(shlex.split(config.start_params))

        return base_args

    async def start(self, server_name: str) -> tuple[bool, str]:
        """Start a server process.

        Args:
            server_name: Name of the server to start.

        Returns:
            A tuple of (success, message).
        """
        lock = self._get_lock(server_name)
        async with lock:
            config = self.get_server(server_name)
            if config is None:
                return False, f"服务器 '{server_name}' 不存在。"

            if self.is_running(server_name):
                return False, f"服务器 '{server_name}' 已经在运行中。"

            try:
                cmd = self._build_start_command(config)
            except ValueError as e:
                return False, str(e)

            # Verify the executable exists
            if not os.path.isfile(cmd[0]):
                return False, f"找不到 srcds 可执行文件: {cmd[0]}"

            try:
                # On Windows, use CREATE_NEW_PROCESS_GROUP to allow clean termination
                creationflags = 0
                if is_windows():
                    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    cwd=str(Path(cmd[0]).parent),
                )
                self._processes[server_name] = proc
                logger.info(
                    "Started L4D2 server '%s' (PID %d): %s",
                    server_name,
                    proc.pid,
                    " ".join(cmd),
                )
                return True, (
                    f"服务器 '{server_name}' 已启动 (PID: {proc.pid})。\n"
                    f"地图: {config.default_map} | 最大玩家: {config.max_players}\n"
                    f"等待服务器就绪后可使用 /l4d2 status 查询状态。"
                )
            except FileNotFoundError:
                return False, f"找不到可执行文件: {cmd[0]}"
            except PermissionError:
                return False, f"没有执行权限: {cmd[0]}"
            except Exception as e:
                logger.exception("Failed to start server '%s'", server_name)
                return False, f"启动服务器失败: {e}"

    async def stop(
        self,
        server_name: str,
        rcon_client: Optional[RconClient] = None,
        force: bool = False,
    ) -> tuple[bool, str]:
        """Stop a server process.

        Attempts graceful shutdown via RCON 'quit' first, then falls back
        to SIGTERM, and finally SIGKILL if the process does not exit.

        Args:
            server_name: Name of the server to stop.
            rcon_client: Optional RCON client for graceful shutdown.
            force: If True, skip RCON graceful shutdown and terminate directly.

        Returns:
            A tuple of (success, message).
        """
        lock = self._get_lock(server_name)
        async with lock:
            proc = self._processes.get(server_name)
            if proc is None or proc.poll() is not None:
                # Process not tracked or already exited
                self._processes.pop(server_name, None)
                return False, f"服务器 '{server_name}' 未在运行。"

            # Attempt graceful shutdown via RCON
            if not force and rcon_client is not None:
                try:
                    await rcon_client.quit()
                    logger.info(
                        "Sent RCON quit to server '%s', waiting for shutdown...",
                        server_name,
                    )
                    # Wait for the process to exit gracefully
                    try:
                        await asyncio.wait_for(
                            asyncio.get_running_loop().run_in_executor(
                                None, proc.wait
                            ),
                            timeout=GRACEFUL_SHUTDOWN_TIMEOUT,
                        )
                        self._processes.pop(server_name, None)
                        return True, f"服务器 '{server_name}' 已优雅停止。"
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Server '%s' did not exit after RCON quit, "
                            "falling back to terminate.",
                            server_name,
                        )
                except Exception as e:
                    logger.warning(
                        "RCON quit failed for server '%s': %s, "
                        "falling back to terminate.",
                        server_name,
                        e,
                    )

            # Force terminate
            try:
                if is_windows():
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGTERM)

                try:
                    await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            None, proc.wait
                        ),
                        timeout=TERMINATE_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Server '%s' did not respond to SIGTERM, killing.",
                        server_name,
                    )
                    proc.kill()
                    proc.wait(timeout=5)

                self._processes.pop(server_name, None)
                return True, f"服务器 '{server_name}' 已停止。"
            except Exception as e:
                logger.exception("Failed to stop server '%s'", server_name)
                return False, f"停止服务器失败: {e}"

    async def restart(
        self,
        server_name: str,
        rcon_client: Optional[RconClient] = None,
    ) -> tuple[bool, str]:
        """Restart a server process.

        Stops the server (if running) and then starts it again.

        Args:
            server_name: Name of the server to restart.
            rcon_client: Optional RCON client for graceful shutdown.

        Returns:
            A tuple of (success, message).
        """
        config = self.get_server(server_name)
        if config is None:
            return False, f"服务器 '{server_name}' 不存在。"

        # Stop if running
        if self.is_running(server_name):
            stop_ok, stop_msg = await self.stop(
                server_name, rcon_client=rcon_client
            )
            if not stop_ok:
                return False, f"重启失败（停止阶段）: {stop_msg}"
            # Brief pause to ensure port is released
            await asyncio.sleep(2)

        # Start
        return await self.start(server_name)

    async def terminate_all(self) -> None:
        """Terminate all managed server processes.

        Called during plugin shutdown to clean up child processes.
        Uses force stop without RCON for quick cleanup.
        """
        for server_name in list(self._processes.keys()):
            if self.is_running(server_name):
                try:
                    proc = self._processes[server_name]
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                    logger.info("Terminated server '%s' during plugin shutdown.", server_name)
                except Exception as e:
                    logger.warning(
                        "Failed to terminate server '%s' during shutdown: %s",
                        server_name,
                        e,
                    )
        self._processes.clear()
