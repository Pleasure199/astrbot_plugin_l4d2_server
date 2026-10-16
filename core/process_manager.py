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
import subprocess
import time
from pathlib import Path

from .models import ServerConfig
from .rcon_client import RconClient
from .ssh_client import SshClient
from .utils import is_windows

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None

logger = logging.getLogger("astrbot")

# Time to wait for graceful shutdown via RCON before force-killing (seconds)
GRACEFUL_SHUTDOWN_TIMEOUT = 15
# Time to wait for process to terminate after SIGTERM (seconds)
TERMINATE_TIMEOUT = 10
# Upper bound for probing SSH servers while the plugin loads (seconds)
SSH_STATE_PROBE_TIMEOUT = 20
# The srcds process name always contains this token (srcds.exe / srcds_run / srcds_linux)
SRCDS_MATCH_TOKEN = "srcds"


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

    def get_server(self, server_name: str) -> ServerConfig | None:
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

        For locally-managed servers, checks the tracked Popen object and falls
        back to searching for the srcds process itself. For SSH-managed servers,
        checks the local running-state set.

        Args:
            server_name: Name of the server.

        Returns:
            True if the process is running, False otherwise.
        """
        config = self.servers.get(server_name)
        if config is not None and config.ssh_enabled:
            return server_name in self._ssh_running
        proc = self._processes.get(server_name)
        if proc is not None and proc.poll() is None:
            return True
        if proc is not None:
            # The launcher exited (e.g. a start.bat that used "start"), so the
            # handle is stale. Drop it and look for the real server process.
            self._processes.pop(server_name, None)
        return bool(self._find_server_processes(server_name))

    def _find_server_processes(self, server_name: str) -> list:
        """Find the local srcds processes that belong to a configured server.

        The tracked ``Popen`` handle alone is not enough: AstrBot reloads the
        plugin whenever its configuration is saved (which drops all in-memory
        state), and a launcher script can exit while the server it spawned keeps
        running. Matching on the server directory makes such servers manageable
        again instead of leaving them unstoppable.

        Args:
            server_name: Name of the server.

        Returns:
            List of matching psutil.Process objects, empty when psutil is
            unavailable, the server is SSH-managed, or nothing matches.
        """
        config = self.servers.get(server_name)
        if psutil is None or config is None or config.ssh_enabled:
            return []
        if not config.srcds_path:
            return []

        server_dir = str(self._resolve_server_dir(config.srcds_path))
        server_dir = server_dir.replace("\\", "/").lower()
        srcds_path = self._normalize_path(config.srcds_path)
        srcds_path = srcds_path.replace("\\", "/").lower()
        port = config.port

        found = []
        for proc in psutil.process_iter(["name", "cmdline", "cwd"]):
            name = (proc.info["name"] or "").lower()
            cmdline = " ".join(proc.info["cmdline"] or [])
            cmdline = cmdline.replace("\\", "/").lower()
            if SRCDS_MATCH_TOKEN not in name and SRCDS_MATCH_TOKEN not in cmdline:
                continue
            cwd = (proc.info["cwd"] or "").replace("\\", "/").lower()
            if (
                cwd == server_dir
                or server_dir in cmdline
                or srcds_path in cmdline
                or f"-port {port}" in cmdline
                or f"-port={port}" in cmdline
                or f"+hostport {port}" in cmdline
            ):
                found.append(proc)
        return found

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

    def get_ssh_client(self, server_name: str) -> SshClient | None:
        """Return an SSH client for a server, if it is SSH-managed.

        Args:
            server_name: Name of the server.

        Returns:
            A configured SshClient, or None when the server does not use SSH.
        """
        config = self.servers.get(server_name)
        if config is None or not config.ssh_enabled:
            return None
        return self._get_ssh_client(config)

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

        server_dir, _pgid = await ssh.read_server_state(raw_path)
        if not server_dir:
            return False, f"无法解析远程服务器目录: {raw_path}"

        # Never start a second instance: the in-memory running state is lost
        # when the plugin is reloaded, so ask the remote host instead.
        if await self._ssh_server_running(config, server_dir):
            self._ssh_running.add(server_name)
            return False, f"服务器 '{server_name}' 已经在运行中。"

        # Check remotely whether start.sh exists in the resolved directory.
        # ``server_dir`` may legitimately contain spaces or quotes, so it must
        # remain one shell argument.
        _, out, _ = await ssh.execute(
            f"test -f {shlex.quote(f'{server_dir}/start.sh')} && echo 1 || echo 0"
        )
        has_start_sh = out.strip() == "1"

        if has_start_sh:
            start_args = ["bash", "start.sh"]
            launch_mode = "启动脚本: start.sh (SSH远程)"
        else:
            start_args = [
                "./srcds_run",
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
                "+exec",
                "server.cfg",
                "-console",
            ]
            launch_mode = "srcds 直接启动 (SSH远程)"

        # Parse additional parameters into arguments before quoting every item.
        # Admins may save presets from chat, so interpolating this text into the
        # remote shell command would grant them arbitrary shell execution.
        if config.start_params:
            try:
                start_args.extend(shlex.split(config.start_params))
            except ValueError as e:
                return False, f"启动参数格式错误: {e}"
        start_cmd = " ".join(shlex.quote(arg) for arg in start_args)

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
        rcon_client: RconClient | None,
        force: bool,
    ) -> tuple[bool, str]:
        """Stop the server on a remote Linux machine via SSH.

        The remote host is authoritative here, because the running-state cache
        only lives in memory and is lost whenever the plugin is reloaded. The
        method first asks the server to quit over RCON, then kills the process
        group that was recorded when the server was started - which also takes
        down any launcher that would restart it - and finally removes whatever
        srcds process is still identified as belonging to this server.

        Args:
            config: Server configuration.
            server_name: Name of the server.
            rcon_client: Optional RCON client for graceful shutdown.
            force: If True, skip RCON graceful shutdown.

        Returns:
            A tuple of (success, message).
        """
        ssh = self._get_ssh_client(config)
        server_dir, pgid = await ssh.read_server_state(config.srcds_path)
        if not server_dir:
            return False, f"无法解析远程服务器目录: {config.srcds_path}"

        processes = await ssh.list_srcds_processes()
        matched = self._match_ssh_processes(server_dir, config.port, processes)
        if not matched:
            self._ssh_running.discard(server_name)
            # A pid file outlives a crash and PIDs can be reused. Never kill a
            # process group based on that file without a current srcds match.
            await ssh.remove_pid_file(server_dir)
            if processes:
                listing = "\n".join(
                    f"  PID {p['pid']} cwd={p['cwd']} args={p['args'][:80]}"
                    for p in processes
                )
                return False, (
                    f"未找到属于服务器 '{server_name}' 的 srcds 进程，"
                    f"因此没有执行任何停止操作。\n"
                    f"远程目录: {server_dir}\n"
                    f"远程主机上现有的 srcds 进程：\n{listing}\n"
                    f"如果其中有你的服务器，请把 srcds_path 改成它的实际运行目录。"
                )
            return False, f"服务器 '{server_name}' 未在运行。"

        # --- Graceful shutdown via RCON ---
        if not force and rcon_client is not None:
            await rcon_client.quit()
            deadline = time.monotonic() + GRACEFUL_SHUTDOWN_TIMEOUT
            while time.monotonic() < deadline:
                if not await self._ssh_server_running(config, server_dir):
                    await ssh.remove_pid_file(server_dir)
                    self._ssh_running.discard(server_name)
                    return True, f"服务器 '{server_name}' 已优雅停止。"
                await asyncio.sleep(3)
            logger.warning(
                "SSH server '%s' did not exit after RCON quit, killing it.",
                server_name,
            )

        # --- Kill the verified process group, then any leftover srcds process ---
        # A recorded PGID is trusted only when an identified srcds process still
        # belongs to it. This prevents a stale pid file from targeting an
        # unrelated process group after PID reuse.
        matched_pgids = {
            int(process["pgid"])
            for process in matched
            if process["pgid"].isdigit()
        }
        if pgid is not None and pgid in matched_pgids:
            await ssh.kill_targets(pgid, [])

        leftovers = self._match_ssh_processes(
            server_dir, config.port, await ssh.list_srcds_processes()
        )
        if leftovers:
            await ssh.kill_targets(None, [p["pid"] for p in leftovers])
            leftovers = self._match_ssh_processes(
                server_dir, config.port, await ssh.list_srcds_processes()
            )

        await ssh.remove_pid_file(server_dir)
        if leftovers:
            pids = ", ".join(p["pid"] for p in leftovers)
            return False, (
                f"服务器 '{server_name}' 仍未停止，以下 srcds 进程还活着：{pids}\n"
                f"远程目录: {server_dir}\n"
                f"请确认 SSH 用户（{config.ssh_username}）有权限结束这些进程。"
            )
        self._ssh_running.discard(server_name)
        return True, f"服务器 '{server_name}' 已通过 SSH 远程停止。"

    @staticmethod
    def _match_ssh_processes(
        server_dir: str, port: int, processes: list[dict]
    ) -> list[dict]:
        """Pick the remote srcds processes that belong to one server.

        Args:
            server_dir: Resolved server directory on the remote host.
            port: Game port of the server, used as a fallback selector.
            processes: Result of ``SshClient.list_srcds_processes``.

        Returns:
            The matching process entries, empty when none belong to the server.
        """
        by_dir = [p for p in processes if server_dir and p["cwd"] == server_dir]
        if by_dir:
            return by_dir
        # A start script may change into another directory, so fall back to the
        # game port, which two servers on the same host cannot share.
        if port:
            needles = (f"-port {port}", f"-port={port}", f"+hostport {port}")
            return [p for p in processes if any(n in p["args"] for n in needles)]
        return []

    async def _ssh_server_running(self, config: ServerConfig, server_dir: str) -> bool:
        """Check on the remote host whether a server still has a live process.

        Args:
            config: Server configuration of an SSH-managed server.
            server_dir: Resolved server directory on the remote host.

        Returns:
            True if a matching srcds process is running. When a process group
            is recorded, the match must belong to that group.
        """
        ssh = self._get_ssh_client(config)
        _server_dir, pgid = await ssh.read_server_state(server_dir)
        processes = await ssh.list_srcds_processes()
        matched = self._match_ssh_processes(server_dir, config.port, processes)
        if pgid is None:
            return bool(matched)
        return any(
            process["pgid"].isdigit() and int(process["pgid"]) == pgid
            for process in matched
        )

    def get_running_servers(self) -> set[str]:
        """Return the set of server names that currently have a running process.

        Includes both locally-managed processes and SSH-managed servers
        tracked in the running-state set.

        Returns:
            Set of running server names.
        """
        names = set(self.servers) | set(self._processes)
        return {name for name in names if self.is_running(name)}

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

    def _find_start_script(self, srcds_path: str) -> Path | None:
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
            raise ValueError(f"服务器 '{config.name}' 未配置 srcds_path，无法启动。")

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

    async def start(self, server_name: str, extra_params: str = "") -> tuple[bool, str]:
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
                # Put the server in its own session / process group so it does
                # not die together with AstrBot (for example on Ctrl+C) and so
                # its children can be terminated as a group.
                creationflags = 0
                if is_windows():
                    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

                # Popen is used instead of asyncio.create_subprocess_exec so the
                # process can be polled and killed synchronously later on.
                proc = subprocess.Popen(  # noqa: ASYNC220
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    start_new_session=not is_windows(),
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
        rcon_client: RconClient | None = None,
        force: bool = False,
    ) -> tuple[bool, str]:
        """Stop a server process.

        Attempts a graceful shutdown via RCON 'quit' first, then force-kills the
        whole process tree. Killing the tracked launcher alone is not enough,
        because a start script spawns the actual server as a child process.

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
                return await self._stop_via_ssh(config, server_name, rcon_client, force)

            if not self.is_running(server_name):
                return False, (
                    f"未在本机检测到服务器 '{server_name}' 的进程，无法停止。\n"
                    f"如果该服务器运行在其它机器上，请为它启用 SSH 远程管理："
                    f"仅配置 RCON 无法结束远端进程。"
                )

            # Attempt a graceful shutdown via RCON. A single attempt is enough
            # for 'quit', and a failure just means the kill below does the work.
            if not force and rcon_client is not None:
                await rcon_client.quit()
                deadline = time.monotonic() + GRACEFUL_SHUTDOWN_TIMEOUT
                # The server is external state, so its exit has to be polled.
                while self.is_running(server_name) and time.monotonic() < deadline:  # noqa: ASYNC110
                    await asyncio.sleep(1)
                if not self.is_running(server_name):
                    self._processes.pop(server_name, None)
                    return True, f"服务器 '{server_name}' 已优雅停止。"
                logger.warning(
                    "Server '%s' did not exit after RCON quit, killing it.",
                    server_name,
                )

            # Force kill the whole process tree
            stopped = await asyncio.get_running_loop().run_in_executor(
                None, self._kill_local_server, server_name
            )
            self._processes.pop(server_name, None)
            if stopped:
                return True, f"服务器 '{server_name}' 已停止。"
            return False, f"停止服务器 '{server_name}' 失败，请检查进程权限。"

    def _kill_local_server(self, server_name: str) -> bool:
        """Force-kill every local process that belongs to a server.

        The tracked child is only a launcher in common setups (``cmd /c
        start.bat`` on Windows, ``start.sh`` on Linux), so its children have to
        be killed as well, otherwise the game server keeps running and cannot be
        reached by stop any more.

        Args:
            server_name: Name of the server to kill.

        Returns:
            True if no process of the server is left running.
        """
        proc = self._processes.get(server_name)
        if proc is not None and proc.poll() is not None:
            self._processes.pop(server_name, None)
            proc = None

        if psutil is None:
            # Without psutil only the directly spawned child can be stopped.
            if proc is None:
                return True
            try:
                proc.kill()
                proc.wait(timeout=TERMINATE_TIMEOUT)
                return True
            except Exception as e:
                logger.warning("Failed to kill server '%s': %s", server_name, e)
                return False

        # Two passes, because a start script that restarts the server would
        # bring it back right after the first round of kills.
        for _attempt in range(2):
            pids = {p.pid for p in self._find_server_processes(server_name)}
            if proc is not None and proc.poll() is None:
                pids.add(proc.pid)

            # Collect the tree up front: once the launcher is gone its children
            # are re-parented and can no longer be found through it.
            targets: dict[int, psutil.Process] = {}
            for pid in pids:
                try:
                    root = psutil.Process(pid)
                except psutil.Error:
                    continue
                targets[root.pid] = root
                try:
                    for child in root.children(recursive=True):
                        targets[child.pid] = child
                except psutil.Error:
                    continue
            if not targets:
                return True

            # Descendants first, so a launcher cannot respawn them.
            ordered = list(reversed(list(targets.values())))
            for target in ordered:
                try:
                    target.terminate()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(ordered, timeout=TERMINATE_TIMEOUT)
            for target in alive:
                try:
                    target.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs(alive, timeout=5)
            if not self._find_server_processes(server_name):
                return True

        remaining = self._find_server_processes(server_name)
        if remaining:
            logger.warning(
                "Could not stop %d process(es) of server '%s': %s",
                len(remaining),
                server_name,
                [p.pid for p in remaining],
            )
            return False
        return True

    async def restart(
        self,
        server_name: str,
        rcon_client: RconClient | None = None,
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
            stop_ok, stop_msg = await self.stop(server_name, rcon_client=rcon_client)
            if not stop_ok:
                return False, f"重启失败（停止阶段）: {stop_msg}"
            # Brief pause to ensure port is released
            await asyncio.sleep(2)

        # Start
        return await self.start(server_name, extra_params=extra_params)

    async def refresh_ssh_states(self) -> None:
        """Re-detect which SSH-managed servers are running on their remote hosts.

        The running-state set only lives in memory, so it is gone after a plugin
        reload (AstrBot reloads the plugin on every config save). Probing the
        remote hosts once keeps status queries and duplicate-start protection
        accurate.
        """
        pending = [
            self._refresh_ssh_state(name, config)
            for name, config in self.servers.items()
            if config.ssh_enabled and config.ssh_host and config.ssh_username
        ]
        if not pending:
            return
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=SSH_STATE_PROBE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out after %ds while probing SSH server states.",
                SSH_STATE_PROBE_TIMEOUT,
            )
            return
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Failed to probe an SSH server state: %s", result)

    async def _refresh_ssh_state(self, server_name: str, config: ServerConfig) -> None:
        """Probe one SSH-managed server and update the running-state set.

        Args:
            server_name: Name of the server.
            config: Server configuration of an SSH-managed server.
        """
        ssh = self._get_ssh_client(config)
        server_dir, pgid = await ssh.read_server_state(config.srcds_path)
        processes = await ssh.list_srcds_processes()
        matched = self._match_ssh_processes(server_dir, config.port, processes)
        if pgid is None:
            running = bool(matched)
        else:
            running = any(
                process["pgid"].isdigit() and int(process["pgid"]) == pgid
                for process in matched
            )
        if running:
            self._ssh_running.add(server_name)
        else:
            self._ssh_running.discard(server_name)
