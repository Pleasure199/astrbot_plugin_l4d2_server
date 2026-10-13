"""SSH client for remote L4D2 server management.

Provides asynchronous wrappers around paramiko for executing commands
on a remote Linux server that hosts the L4D2 dedicated server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("astrbot")

try:
    import paramiko
except ImportError:
    paramiko = None


class SshClient:
    """A simple SSH client for remote server process management.

    Uses paramiko under the hood, wrapped in asyncio executors for
    non-blocking operation within AstrBot's async event loop.

    Attributes:
        host: SSH server hostname or IP.
        port: SSH port.
        username: SSH login username.
        password: SSH password (or key passphrase).
        key_path: Path to a private key file (alternative to password).
        timeout: Connection and command timeout in seconds.
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "root",
        password: str = "",
        key_path: str = "",
        timeout: int = 15,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_path = key_path
        self.timeout = timeout
        self._client: Optional["paramiko.SSHClient"] = None

    def _connect_sync(self) -> "paramiko.SSHClient":
        """Open an SSH connection (synchronous, run in executor).

        Returns:
            A connected paramiko SSHClient.

        Raises:
            RuntimeError: If paramiko is not installed.
            paramiko.SSHException: If authentication or connection fails.
        """
        if paramiko is None:
            raise RuntimeError(
                "paramiko is not installed. Run: pip install paramiko"
            )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": self.timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }

        if self.key_path:
            connect_kwargs["key_filename"] = self.key_path
            if self.password:
                connect_kwargs["passphrase"] = self.password
        elif self.password:
            connect_kwargs["password"] = self.password

        client.connect(**connect_kwargs)
        return client

    async def execute(self, command: str) -> tuple[int, str, str]:
        """Execute a command on the remote server and wait for completion.

        Args:
            command: Shell command to execute.

        Returns:
            A tuple of (exit_code, stdout, stderr).

        Raises:
            RuntimeError: If paramiko is not installed.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._execute_sync, command)

    async def execute_no_wait(self, command: str) -> None:
        """Send a command to the remote server without waiting for completion.

        Useful for launching long-running background processes where waiting
        for the SSH channel to close would block indefinitely.

        Args:
            command: Shell command to execute.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._execute_no_wait_sync, command)

    def _execute_no_wait_sync(self, command: str) -> None:
        """Send a command without reading output or exit status.

        Args:
            command: Shell command to execute.
        """
        client = self._connect_sync()
        try:
            client.exec_command(command, timeout=self.timeout)
        finally:
            client.close()

    def _execute_sync(self, command: str) -> tuple[int, str, str]:
        """Execute a command synchronously.

        Args:
            command: Shell command to execute.

        Returns:
            A tuple of (exit_code, stdout, stderr).
        """
        client = self._connect_sync()
        try:
            _, stdout, stderr = client.exec_command(command, timeout=self.timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            return exit_code, out, err
        finally:
            client.close()

    async def start_server(
        self, server_dir: str, start_command: str
    ) -> tuple[bool, str]:
        """Start the L4D2 server process on the remote machine.

        Launches the server with nohup so it survives SSH disconnect.

        Args:
            server_dir: Working directory for the server.
            start_command: The command to start the server (e.g. './start.sh').

        Returns:
            A tuple of (success, message).
        """
        # Use setsid for full session detachment, redirect all FDs so the
        # SSH channel can close immediately without waiting on the child.
        remote_cmd = (
            f"cd {server_dir} && setsid nohup {start_command} "
            f"> /dev/null 2>&1 < /dev/null &"
        )
        try:
            await self.execute_no_wait(remote_cmd)
        except Exception as e:
            return False, f"启动命令发送失败: {e}"

        # Give the process a moment to spawn, then verify it is running
        await asyncio.sleep(3)
        if await self.is_running():
            return True, "远程服务器已启动"
        return False, (
            "启动命令已发送，但未检测到 srcds 进程。"
            "请检查 start.sh 是否有执行权限、参数是否正确。"
        )

    async def stop_server(self, server_dir: str) -> tuple[bool, str]:
        """Stop the L4D2 server process on the remote machine.

        Attempts graceful shutdown via RCON should be handled by the caller.
        This method force-kills srcds processes as a fallback.

        Args:
            server_dir: Server directory (used for process matching context).

        Returns:
            A tuple of (success, message).
        """
        # Match all possible L4D2 process names:
        # srcds_linux (actual game binary), srcds_run (launcher script),
        # srcds.exe (Windows). Send SIGTERM first, then SIGKILL if needed.
        pattern = "srcds_linux|srcds_run|srcds.exe"
        remote_cmd = (
            f"pkill -f '{pattern}' 2>/dev/null; "
            f"sleep 3; "
            f"if pgrep -f '{pattern}' > /dev/null 2>&1; then "
            f"pkill -9 -f '{pattern}' 2>/dev/null; sleep 1; fi; "
            f"if pgrep -f '{pattern}' > /dev/null 2>&1; then echo 'still_running'; "
            f"else echo 'stopped'; fi"
        )
        try:
            _, out, err = await self.execute(remote_cmd)
        except Exception as e:
            return False, f"SSH 执行停止命令失败: {e}"

        result = out.strip()
        if result == "stopped":
            return True, "远程服务器进程已停止"
        if result == "still_running":
            return False, "进程未能终止，可能需要手动 kill"
        return False, f"停止命令异常: {err or out or '未知错误'}"

    async def is_running(self) -> bool:
        """Check if a srcds process is running on the remote machine.

        Returns:
            True if at least one srcds process is found.
        """
        pattern = "srcds_linux|srcds_run|srcds.exe"
        remote_cmd = f"pgrep -f '{pattern}' > /dev/null && echo 1 || echo 0"
        try:
            _, out, _ = await self.execute(remote_cmd)
            return out.strip() == "1"
        except Exception:
            return False

    async def test_connection(self) -> tuple[bool, str]:
        """Test the SSH connection and authentication.

        Returns:
            A tuple of (success, message).
        """
        try:
            exit_code, out, _ = await self.execute("echo connected")
            if exit_code == 0 and "connected" in out:
                return True, "SSH 连接成功"
            return False, f"SSH 连接异常 (exit code: {exit_code})"
        except Exception as e:
            return False, f"SSH 连接失败: {e}"
