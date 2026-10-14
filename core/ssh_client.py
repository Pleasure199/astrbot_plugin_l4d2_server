"""SSH client for remote L4D2 server management.

Provides asynchronous wrappers around paramiko for executing commands
on a remote Linux server that hosts the L4D2 dedicated server.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("astrbot")

try:
    import paramiko
except ImportError:
    paramiko = None

# Token shared by every srcds executable name (srcds.exe / srcds_run / srcds_linux)
SRCDS_NAME = "srcds"
# File the plugin writes next to the server to remember its process group
REMOTE_PID_FILE = ".astrbot_l4d2.pid"
# Smallest slice of the budget handed to a single tunnel attempt
MIN_TUNNEL_TIMEOUT = 1.0


def _quote(text: str) -> str:
    """Wrap a string in single quotes for safe use in a POSIX shell command.

    Args:
        text: Raw text to quote.

    Returns:
        The text wrapped in single quotes, with embedded quotes escaped.
    """
    return "'" + text.replace("'", "'\\''") + "'"


def _srcds_pattern() -> str:
    """Build the shell snippet that stores the srcds search pattern in ``$pat``.

    The pattern is assembled from a variable so that it never appears literally
    in the command line. Otherwise the shell that runs ``pgrep -f "$pat"`` would
    find itself and stand a good chance of killing its own session.

    Returns:
        A shell snippet that ends with the search pattern in ``$pat``.
    """
    return f'a={SRCDS_NAME}; pat="${{a}}_linux|${{a}}_run|${{a}}.exe"'


class _TunnelSocket:
    """Socket-like wrapper around an SSH channel that owns its SSH client.

    The RCON client only needs ``recv``, ``sendall``, ``settimeout``,
    ``gettimeout`` and ``close``, all of which a paramiko channel provides, so a
    tunnelled connection can be used exactly like a plain TCP socket.
    """

    def __init__(self, ssh_client: paramiko.SSHClient, channel) -> None:
        """Initialize the wrapper.

        Args:
            ssh_client: The SSH client the channel belongs to.
            channel: The open paramiko channel.
        """
        self._client = ssh_client
        self._channel = channel

    def recv(self, count: int) -> bytes:
        """Receive up to ``count`` bytes from the tunnel."""
        return self._channel.recv(count)

    def sendall(self, data: bytes) -> None:
        """Send all bytes through the tunnel."""
        self._channel.sendall(data)

    def settimeout(self, timeout: float) -> None:
        """Set the read timeout of the tunnel."""
        self._channel.settimeout(timeout)

    def gettimeout(self) -> float | None:
        """Return the read timeout of the tunnel."""
        return self._channel.gettimeout()

    def close(self) -> None:
        """Close the channel and the SSH client it was opened on."""
        try:
            self._channel.close()
        finally:
            self._client.close()


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
        self._client: paramiko.SSHClient | None = None

    def _connect_sync(self, timeout: float | None = None) -> paramiko.SSHClient:
        """Open an SSH connection (synchronous, run in executor).

        Args:
            timeout: Connection timeout in seconds. Defaults to the client
                timeout; callers can pass a smaller budget, for example when a
                tunnel has to fit into the RCON timeout.

        Returns:
            A connected paramiko SSHClient.

        Raises:
            RuntimeError: If paramiko is not installed.
            paramiko.SSHException: If authentication or connection fails.
        """
        if paramiko is None:
            raise RuntimeError("paramiko is not installed. Run: pip install paramiko")

        budget = timeout if timeout is not None else self.timeout
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": budget,
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

    def _execute_sync(
        self, command: str, timeout: float | None = None
    ) -> tuple[int, str, str]:
        """Execute a command synchronously.

        Args:
            command: Shell command to execute.
            timeout: Connection timeout in seconds, defaults to the client
                timeout.

        Returns:
            A tuple of (exit_code, stdout, stderr).
        """
        budget = timeout if timeout is not None else self.timeout
        client = self._connect_sync(budget)
        try:
            _, stdout, stderr = client.exec_command(command, timeout=budget)
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

        Launches the server with setsid/nohup so it survives SSH disconnects and
        gets a process group of its own. That group id is written to a pid file
        next to the server, so the server - including any launcher that would
        restart it - can be stopped reliably later.

        Args:
            server_dir: Working directory for the server.
            start_command: The command to start the server (e.g. './start.sh').

        Returns:
            A tuple of (success, message).
        """
        pid_path = f"{server_dir}/{REMOTE_PID_FILE}"
        # "cd" is deliberately a statement of its own: "cd x && cmd &" would
        # background the whole list, so "$!" would not be the server's pid.
        # All file descriptors are redirected so the SSH channel can close
        # immediately without waiting for the server.
        remote_cmd = (
            f"cd {_quote(server_dir)} || exit 1; "
            f"setsid nohup {start_command} > /dev/null 2>&1 < /dev/null & "
            f'pid=$!; echo "$pid" > {_quote(pid_path)}; echo "$pid"'
        )
        try:
            _, out, _ = await self.execute(remote_cmd)
        except Exception as e:
            return False, f"启动命令发送失败: {e}"

        await asyncio.sleep(3)
        pid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if pid.isdigit() and await self.pid_alive(int(pid)):
            return True, f"远程服务器已启动 (PID: {pid})"
        return False, (
            "启动命令已发送，但未检测到服务器进程。"
            "请检查 start.sh 是否有执行权限、参数是否正确。"
        )

    async def read_server_state(self, srcds_path: str) -> tuple[str, int | None]:
        """Resolve the server directory and read its recorded process group.

        Both pieces live on the remote host and are needed together, so they are
        fetched with a single command.

        Args:
            srcds_path: Configured srcds path, which may point at a directory or
                at a file inside it (for example ``start.sh``).

        Returns:
            A tuple of (resolved server directory, recorded process group id).
            The directory is empty when it cannot be determined, and the process
            group is None when no record exists.
        """
        remote_cmd = (
            f"d={_quote(srcds_path)}; "
            'if [ ! -d "$d" ]; then d=$(dirname "$d"); fi; '
            'd=$(readlink -f "$d" 2>/dev/null); '
            'echo "$d"; '
            f'cat "$d/{REMOTE_PID_FILE}" 2>/dev/null'
        )
        try:
            _, out, _ = await self.execute(remote_cmd)
        except Exception as e:
            logger.warning("Failed to read remote state of %s: %s", srcds_path, e)
            return "", None
        lines = out.splitlines()
        server_dir = lines[0].strip() if lines else ""
        pid_text = lines[1].strip() if len(lines) > 1 else ""
        return server_dir, (int(pid_text) if pid_text.isdigit() else None)

    async def list_srcds_processes(self) -> list[dict[str, str]]:
        """List the srcds processes running on the remote host.

        Returns:
            One dict per process with the keys ``pid``, ``pgid``, ``cwd`` and
            ``args``. The list is empty when nothing matches or the host cannot
            be reached.
        """
        remote_cmd = (
            f"{_srcds_pattern()}; "
            'for pid in $(pgrep -f "$pat" 2>/dev/null); do '
            "pgid=$(cut -d' ' -f5 /proc/$pid/stat 2>/dev/null); "
            "cwd=$(readlink /proc/$pid/cwd 2>/dev/null); "
            "args=$(tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null); "
            'printf \'proc|%s|%s|%s|%s\\n\' "$pid" "$pgid" "$cwd" "$args"; '
            "done; echo end"
        )
        try:
            _, out, _ = await self.execute(remote_cmd)
        except Exception as e:
            logger.warning("Failed to list srcds processes: %s", e)
            return []

        processes = []
        for line in out.splitlines():
            if not line.startswith("proc|"):
                continue
            parts = line.split("|", 4)
            if len(parts) != 5:
                continue
            processes.append(
                {
                    "pid": parts[1].strip(),
                    "pgid": parts[2].strip(),
                    "cwd": parts[3].strip(),
                    "args": parts[4].strip(),
                }
            )
        return processes

    async def remove_pid_file(self, server_dir: str) -> None:
        """Delete the recorded process group file of a server.

        Args:
            server_dir: Resolved remote server directory.
        """
        pid_path = f"{server_dir}/{REMOTE_PID_FILE}"
        try:
            await self.execute(f"rm -f {_quote(pid_path)}")
        except Exception as e:
            logger.debug("Failed to remove pid file %s: %s", pid_path, e)

    async def pid_alive(self, pid: int) -> bool:
        """Check whether a process is still alive on the remote host.

        Args:
            pid: Process id to check.

        Returns:
            True if the process still exists.
        """
        try:
            _, out, _ = await self.execute(
                f"kill -0 {int(pid)} 2>/dev/null && echo 1 || echo 0"
            )
        except Exception as e:
            logger.warning("Failed to check remote pid %s: %s", pid, e)
            return False
        return out.strip() == "1"

    async def kill_targets(self, pgid: int | None, pids: list[str]) -> None:
        """Terminate the given processes on the remote host.

        Killing the whole process group also takes down any launcher that would
        restart the server right after it dies.

        Args:
            pgid: Process group id of the server, if it is known.
            pids: Process ids to terminate as well.
        """
        term = []
        kill = []
        if pgid:
            term.append(f"pkill -TERM -g {int(pgid)} 2>/dev/null")
            kill.append(f"pkill -KILL -g {int(pgid)} 2>/dev/null")
        # Only digits are ever passed on, so nothing from the remote process
        # listing can turn into shell syntax.
        pid_args = " ".join(str(pid) for pid in pids if str(pid).isdigit())
        if pid_args:
            term.append(f"kill -TERM {pid_args} 2>/dev/null")
            kill.append(f"kill -KILL {pid_args} 2>/dev/null")
        if not term:
            return
        remote_cmd = f"{'; '.join(term)}; sleep 3; {'; '.join(kill)}; true"
        try:
            await self.execute(remote_cmd)
        except Exception as e:
            logger.warning("Failed to kill remote processes: %s", e)

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

    def open_tunnel(
        self, host: str, port: int, timeout: float | None = None
    ) -> _TunnelSocket:
        """Open a TCP tunnel to ``host:port`` through this SSH connection.

        Many hosts expose the game port but not the RCON port, so the RCON
        client is given a tunnel instead of a direct socket. The destination is
        resolved on the remote host, which means ``127.0.0.1`` refers to the
        SSH host itself.

        Args:
            host: Destination host as seen from the remote host.
            port: Destination RCON port.
            timeout: Budget in seconds for the whole SSH connection and channel
                setup. Defaults to the client timeout.

        Returns:
            A socket-like object connected to the destination.

        Raises:
            RuntimeError: If paramiko is not installed.
            RuntimeError: If the SSH connection or the forwarding fails, with
                the destination included in the message.
        """
        budget = timeout if timeout is not None else self.timeout
        try:
            client = self._connect_sync(budget)
        except Exception as e:
            raise RuntimeError(f"SSH 连接 {self.host}:{self.port} 失败：{e}") from e
        try:
            transport = client.get_transport()
            channel = transport.open_channel(
                "direct-tcpip",
                (host, int(port)),
                ("127.0.0.1", 0),
                timeout=budget,
            )
        except Exception as e:
            client.close()
            # "Connect failed" means the SSH host could not reach the
            # destination, so the destination has to be named here.
            raise RuntimeError(f"SSH 转发到 {host}:{int(port)} 失败：{e}") from e
        return _TunnelSocket(client, channel)

    def rcon_tunnel_targets(self, port: int, timeout: float | None = None) -> list[str]:
        """List the addresses to try when tunnelling to a listening RCON port.

        A game server normally listens on all interfaces or on localhost, which
        ``127.0.0.1`` covers. A server started with an explicit ``-ip`` only
        listens on that single address, so the addresses reported for the port
        are added as further candidates.

        Args:
            port: RCON port that is expected to be listening on the remote host.
            timeout: Budget in seconds for reading the listening sockets.

        Returns:
            Candidate addresses, ``127.0.0.1`` first. Falls back to
            ``["127.0.0.1"]`` when the listening sockets cannot be read.
        """
        targets = ["127.0.0.1"]
        for entry in self._read_listen_addresses(timeout):
            address, _, entry_port = entry.rpartition(":")
            if entry_port != str(int(port)):
                continue
            address = address.strip("[]") or "127.0.0.1"
            if address in ("*", "0.0.0.0", "::"):
                address = "127.0.0.1"
            if address not in targets:
                targets.append(address)
        return targets

    def _read_listen_addresses(self, timeout: float | None = None) -> list[str]:
        """Read the local addresses of the listening TCP sockets (blocking).

        Called from the tunnel opener, which already runs in a worker thread.

        Args:
            timeout: Budget in seconds for the SSH connection.

        Returns:
            Entries such as ``172.24.55.117:10721`` as reported by ``ss`` (or
            ``netstat``), empty when neither tool can be used.
        """
        try:
            _code, out, _err = self._execute_sync(
                "ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null", timeout
            )
        except Exception as e:
            logger.warning("Failed to read listening sockets: %s", e)
            return []
        addresses = []
        for line in out.splitlines():
            fields = line.split()
            # Both ss and netstat put the local address in the fourth column.
            if len(fields) >= 4 and ":" in fields[3]:
                addresses.append(fields[3])
        return addresses

    def open_rcon_tunnel(
        self, port: int, timeout: float | None = None, *, host: str = ""
    ) -> _TunnelSocket:
        """Open a tunnel to the RCON port of a server running on this host.

        Args:
            port: RCON port on the remote host.
            timeout: Budget in seconds for the whole setup.
            host: Explicit destination address. When empty, the addresses the
                port is actually listening on are used.

        Returns:
            A socket-like object connected to the RCON port.

        Raises:
            RuntimeError: If no address could be reached, listing every address
                that was tried.
        """
        budget = timeout if timeout is not None else self.timeout
        targets = [host] if host else self.rcon_tunnel_targets(port, budget)
        deadline = time.monotonic() + budget
        failures = []
        for target in targets:
            remaining = max(deadline - time.monotonic(), MIN_TUNNEL_TIMEOUT)
            try:
                return self.open_tunnel(target, port, remaining)
            except Exception as e:
                failures.append(f"{target}: {e}")
        raise RuntimeError(
            f"无法通过 SSH 转发到 RCON 端口 {int(port)}"
            f"（已尝试 {'、'.join(targets)}）：" + "；".join(failures)
        )
