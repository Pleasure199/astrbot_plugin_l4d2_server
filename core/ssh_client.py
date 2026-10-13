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
        """Execute a command on the remote server.

        Args:
            command: Shell command to execute.

        Returns:
            A tuple of (exit_code, stdout, stderr).

        Raises:
            RuntimeError: If paramiko is not installed.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._execute_sync, command)

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
        # Use nohup and redirect output so the process survives disconnection
        remote_cmd = (
            f"cd {server_dir} && nohup {start_command} "
            f"> /dev/null 2>&1 & echo $!"
        )
        exit_code, out, err = await self.execute(remote_cmd)
        if exit_code == 0 and out:
            pid = out.strip().split("\n")[-1]
            return True, f"远程服务器已启动 (PID: {pid})"
        return False, f"启动失败: {err or '未知错误'}"

    async def stop_server(self, server_dir: str) -> tuple[bool, str]:
        """Stop the L4D2 server process on the remote machine.

        Attempts graceful shutdown via RCON should be handled by the caller.
        This method force-kills srcds processes as a fallback.

        Args:
            server_dir: Server directory (used for process matching context).

        Returns:
            A tuple of (success, message).
        """
        # Kill all srcds_run / srcds processes
        remote_cmd = "pkill -f 'srcds_run|srcds.exe' 2>/dev/null; echo $?"
        exit_code, out, _ = await self.execute(remote_cmd)
        # pkill returns 0 if processes were signaled, 1 if none found
        if out.strip() in ("0", "1"):
            return True, "已发送停止信号到远程服务器进程"
        return False, "停止命令执行失败"

    async def is_running(self) -> bool:
        """Check if a srcds process is running on the remote machine.

        Returns:
            True if at least one srcds process is found.
        """
        remote_cmd = "pgrep -f 'srcds_run|srcds.exe' > /dev/null && echo 1 || echo 0"
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
