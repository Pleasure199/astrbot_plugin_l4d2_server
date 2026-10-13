"""L4D2 dedicated server process manager.

Handles starting, stopping, restarting, and monitoring srcds processes
across Windows and Linux platforms.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Optional

from .models import ServerConfig
from .rcon_client import RconClient
from .ssh_client import SshClient
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
        self._ssh_running: set[str] = set()

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

        For locally-managed servers, checks the tracked Popen object.
        For SSH-managed servers, checks the local running-state set.

        Args:
            server_name: Name of the server.

        Returns:
            True if the process is running, False otherwise.
        """
        config = self.servers.get(server_name)
        if config is not None and config.ssh_enabled:
            return server_name in self._ssh_running
        proc = self._processes.get(server_name)
        if proc is None:
            return False
        return proc.poll() is None

    def _get_ssh_client(self, config: ServerConfig) -> SshClient:
        """Create an SshClient from a server's SSH configuration.

        Args:
            config: Server configuration with SSH fields populated.

        Returns:
            A configured SshClient instance.
        """
        return SshClient(
            host=config.ssh_host,
            port=config.ssh_port,
            username=config.ssh_username,
            password=config.ssh_password,
            key_path=config.ssh_key_path,
        )

    async def _start_via_ssh(
        self, config: ServerConfig, server_name: str
    ) -> tuple[bool, str]:
        """Start the server on a remote Linux machine via SSH.

        Resolves the server directory, checks for a start.sh script remotely,
        and launches the server with nohup so it survives disconnection.

        Args:
            config: Server configuration (with extra_params already merged).
            server_name: Name of the server (for messages and state tracking).

        Returns:
            A tuple of (success, message).
        """
        if not config.ssh_host or not config.ssh_username:
            return False, (
                f"服务器 '{server_name}' 启用了 SSH 模式，但未配置 ssh_host 或 ssh_username。"
            )

        raw_path = config.srcds_path.replace("\\", "/")
        if not raw_path:
            return False, f"服务器 '{server_name}' 未配置 srcds_path（服务器目录）。"

        ssh = self._get_ssh_client(config)

        # Test connection first
        ok, msg = await ssh.test_connection()
        if not ok:
            return False, f"SSH 连接失败: {msg}"

        # Remotely resolve the server directory: if srcds_path is a file,
        # take its parent; if it's a directory, use it as-is.
        _, out, _ = await ssh.execute(
            f'if [ -d "{raw_path}" ]; then echo "{raw_path}"; else dirname "{raw_path}"; fi'
        )
        server_dir = out.strip()
        if not server_dir or server_dir == ".":
            return False, f"无法解析远程服务器目录: {raw_path}"

        # Check remotely whether start.sh exists in the resolved directory
        _, out, _ = await ssh.execute(f'test -f "{server_dir}/start.sh" && echo 1 || echo 0')
        has_start_sh = out.strip() == "1"

        if has_start_sh:
            start_cmd = "bash start.sh"
            launch_mode = "启动脚本: start.sh (SSH远程)"
        else:
            start_cmd = (
                f"./srcds_run -game left4dead2 -port {config.port} "
                f"+hostport {config.port} -maxplayers {config.max_players} "
                f"+map {config.default_map} +exec server.cfg -console"
            )
            launch_mode = "srcds 直接启动 (SSH远程)"

        # Append user-defined start parameters
        if config.start_params:
            start_cmd = f"{start_cmd} {config.start_params}"

        ok, msg = await ssh.start_server(server_dir, start_cmd)
        if not ok:
            return False, f"远程启动失败: {msg}"

        self._ssh_running.add(server_name)
        logger.info(
            "Started L4D2 server '%s' remotely via SSH: %s",
            server_name,
            start_cmd,
        )
        return True, (
            f"服务器 '{server_name}' 已通过 SSH 远程启动。\n"
            f"启动方式: {launch_mode}\n"
            f"远程目录: {server_dir}\n"
            f"地图: {config.default_map} | 最大玩家: {config.max_players}\n"
            f"等待服务器就绪后可使用 /l4d2 status 查询状态。"
        )

    async def _stop_via_ssh(
        self,
        config: ServerConfig,
        server_name: str,
        rcon_client: Optional[RconClient],
        force: bool,
    ) -> tuple[bool, str]:
        """Stop the server on a remote Linux machine via SSH.

        Attempts graceful shutdown via RCON 'quit' first, then falls back
        to SSH pkill. Updates the local SSH running-state set.

        Args:
            config: Server configuration.
            server_name: Name of the server.
            rcon_client: Optional RCON client for graceful shutdown.
            force: If True, skip RCON graceful shutdown.

        Returns:
            A tuple of (success, message).
        """
        if server_name not in self._ssh_running:
            return False, f"服务器 '{server_name}' 未在运行。"

        # Attempt graceful shutdown via RCON
        if not force and rcon_client is not None:
            try:
                await rcon_client.quit()
                logger.info(
                    "Sent RCON quit to SSH server '%s', waiting for shutdown...",
                    server_name,
                )
                await asyncio.sleep(GRACEFUL_SHUTDOWN_TIMEOUT)
            except Exception as e:
                logger.warning(
                    "RCON quit failed for SSH server '%s': %s, "
                    "falling back to pkill.",
                    server_name,
                    e,
                )

        # Force kill via SSH
        ssh = self._get_ssh_client(config)
        ok, msg = await ssh.stop_server(config.srcds_path)
        self._ssh_running.discard(server_name)
        if ok:
            return True, f"服务器 '{server_name}' 已通过 SSH 远程停止。"
        return False, f"远程停止失败: {msg}"

    def get_running_servers(self) -> set[str]:
        """Return the set of server names that are currently running.

        Includes both locally-managed processes and SSH-managed servers
        tracked in the running-state set.

        Returns:
            Set of running server names.
        """
        local = {
            name
            for name in self._processes
            if self._processes[name].poll() is None
        }
        return local | self._ssh_running

    @staticmethod
    def _normalize_path(path_str: str) -> str:
        """Normalize path separators for the current platform.

        On non-Windows platforms, converts backslashes to forward slashes
        to handle paths copied from Windows Explorer (e.g. ``\\home\\l4d2``
        becomes ``/home/l4d2``).

        Args:
            path_str: Raw path string from configuration.

        Returns:
            Normalized path string.
        """
        if not is_windows():
            return path_str.replace("\\", "/")
        return path_str

    def _resolve_server_dir(self, srcds_path: str) -> Path:
        """Resolve the server directory from a srcds_path config value.

        Accepts either a path to the srcds executable (uses its parent)
        or a path to the server directory itself (used as-is). Paths with
        Windows-style backslashes are normalized on non-Windows platforms.

        Args:
            srcds_path: Configured srcds path, may be a file or directory.

        Returns:
            The resolved server directory Path.
        """
        path = Path(self._normalize_path(srcds_path))
        return path if path.is_dir() else path.parent

    def _find_start_script(self, srcds_path: str) -> Optional[Path]:
        """Find a startup script in the server directory.

        On Linux/macOS checks for ``start.sh``. On Windows checks for
        ``start.bat`` first, then falls back to ``start.sh`` (for Git Bash
        / WSL setups). The ``srcds_path`` may point to either the srcds
        executable or the server directory.

        Args:
            srcds_path: Path to the srcds executable or server directory.

        Returns:
            Path to the start script if found, None otherwise.
        """
        server_dir = self._resolve_server_dir(srcds_path)
        if is_windows():
            for script_name in ("start.bat", "start.sh"):
                script_path = server_dir / script_name
                if script_path.is_file():
                    return script_path
            return None
        script_path = server_dir / "start.sh"
        return script_path if script_path.is_file() else None

    def _build_start_command(self, config: ServerConfig) -> tuple[list[str], Path]:
        """Build the command-line arguments and working directory for starting the server.

        If a ``start.sh`` (Linux) or ``start.bat`` / ``start.sh`` (Windows)
        script exists in the server directory, it is used preferentially.
        Otherwise the command is built from the srcds executable and configured
        parameters.

        Args:
            config: Server configuration.

        Returns:
            A tuple of (command_args, working_directory).

        Raises:
            ValueError: If srcds_path is not configured, or if srcds_path
                points to a directory without a start script.
        """
        if not config.srcds_path:
            raise ValueError(
                f"服务器 '{config.name}' 未配置 srcds_path，无法启动。"
            )

        # Prefer start.sh / start.bat if present in the server directory
        start_script = self._find_start_script(config.srcds_path)
        if start_script is not None:
            if is_windows() and start_script.suffix == ".bat":
                cmd = ["cmd", "/c", str(start_script)]
            else:
                cmd = ["/bin/bash", str(start_script)]
            # Pass user-defined start parameters to the script
            if config.start_params:
                cmd.extend(shlex.split(config.start_params))
            return cmd, start_script.parent

        # No start script found: fall back to direct srcds invocation
        srcds = Path(self._normalize_path(config.srcds_path))
        if srcds.is_dir():
            raise ValueError(
                f"服务器 '{config.name}' 的 srcds_path 指向目录 '{srcds}'，"
                f"但该目录下未找到 start.sh（Linux）或 start.bat（Windows）。\n"
                f"请在该目录下放置启动脚本，或将 srcds_path 改为 srcds 可执行文件的完整路径。"
            )

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

        return base_args, srcds.parent

    async def start(
        self, server_name: str, extra_params: str = ""
    ) -> tuple[bool, str]:
        """Start a server process.

        Args:
            server_name: Name of the server to start.
            extra_params: Additional start parameters appended after the
                configured start_params. Useful for saved presets.

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

            # Merge extra params into a config copy
            if extra_params:
                merged = config.start_params
                if merged:
                    merged = f"{merged} {extra_params.strip()}"
                else:
                    merged = extra_params.strip()
                config = dataclasses.replace(config, start_params=merged)

            # --- SSH remote mode ---
            if config.ssh_enabled:
                return await self._start_via_ssh(config, server_name)

            try:
                cmd, work_dir = self._build_start_command(config)
            except ValueError as e:
                return False, str(e)

            # Determine launch mode for the user-facing message
            start_script = self._find_start_script(config.srcds_path)
            using_script = start_script is not None
            launch_mode = (
                f"启动脚本: {start_script.name}" if using_script else "srcds 直接启动"
            )

            # Verify the primary executable exists (for srcds mode only;
            # /bin/bash and cmd are always present on their respective platforms)
            if not using_script and not os.path.isfile(cmd[0]):
                server_dir = self._resolve_server_dir(config.srcds_path)
                script_name = "start.bat / start.sh" if is_windows() else "start.sh"
                normalized = self._normalize_path(config.srcds_path)
                sep_hint = ""
                if "\\" in config.srcds_path and not is_windows():
                    sep_hint = (
                        f"\n  3. 检测到路径使用了反斜杠 (\\)，在 Linux 上请使用正斜杠 (/)："
                        f"\n     原路径: {config.srcds_path}"
                        f"\n     规范化后: {normalized}"
                    )
                return False, (
                    f"找不到 srcds 可执行文件: {cmd[0]}\n"
                    f"已检查的服务器目录: {server_dir}\n"
                    f"该目录下未找到 {script_name} 启动脚本。\n"
                    f"请确认：\n"
                    f"  1. srcds_path 指向正确的 srcds 可执行文件，或服务器目录\n"
                    f"  2. 若使用启动脚本，确保脚本名为 {script_name} 且位于服务器目录下"
                    f"{sep_hint}"
                )

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
                    cwd=str(work_dir),
                )
                self._processes[server_name] = proc
                logger.info(
                    "Started L4D2 server '%s' (PID %d) via %s: %s",
                    server_name,
                    proc.pid,
                    launch_mode,
                    " ".join(cmd),
                )
                return True, (
                    f"服务器 '{server_name}' 已启动 (PID: {proc.pid})。\n"
                    f"启动方式: {launch_mode}\n"
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
            config = self.get_server(server_name)

            # --- SSH remote mode ---
            if config is not None and config.ssh_enabled:
                return await self._stop_via_ssh(
                    config, server_name, rcon_client, force
                )

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
        extra_params: str = "",
    ) -> tuple[bool, str]:
        """Restart a server process.

        Stops the server (if running) and then starts it again.

        Args:
            server_name: Name of the server to restart.
            rcon_client: Optional RCON client for graceful shutdown.
            extra_params: Additional start parameters for the new process.

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
        return await self.start(server_name, extra_params=extra_params)

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
