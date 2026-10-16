"""Utility functions for L4D2 server management plugin."""

from __future__ import annotations

import platform
import re

from .models import PlayerInfo, ServerStatus


def is_windows() -> bool:
    """Check if the current operating system is Windows.

    Returns:
        True if running on Windows, False otherwise.
    """
    return platform.system() == "Windows"


def is_linux() -> bool:
    """Check if the current operating system is Linux.

    Returns:
        True if running on Linux, False otherwise.
    """
    return platform.system() == "Linux"


def parse_status_output(raw: str) -> ServerStatus:
    """Parse the raw output of the L4D2 'status' RCON command.

    The status output format varies slightly between engine versions but
    generally follows the pattern:
        hostname: ...
        version : ...
        map     : ... at ...
        players : N humans, N bots
        # userid name uniqueid connected ping loss state rate adr

    Args:
        raw: Raw text output from the 'status' command.

    Returns:
        A ServerStatus object with parsed fields.
    """
    status = ServerStatus(raw=raw)
    lines = raw.splitlines()

    for line in lines:
        stripped = line.strip()

        # hostname
        match = re.match(r"hostname\s*:\s*(.+)", stripped)
        if match:
            status.hostname = match.group(1).strip()
            continue

        # version
        match = re.match(r"version\s*:\s*(.+)", stripped)
        if match:
            status.version = match.group(1).strip()
            continue

        # map
        match = re.match(r"map\s*:\s*(\S+)", stripped)
        if match:
            status.map = match.group(1).strip()
            continue

        # players count: "players : 2 humans, 0 bots (8 max)"
        match = re.match(
            r"players\s*:\s*(\d+)\s+humans.*?(\d+)\s+max",
            stripped,
        )
        if match:
            status.player_count = int(match.group(1))
            status.max_players = int(match.group(2))
            continue

        # L4D2 inserts a numeric slot between userid and the quoted name.
        # Some status variants omit rate/address. BOT rows have no connection
        # metrics and are excluded to match the human count in the summary.
        match = re.fullmatch(
            r'#\s*(\d+)\s+(?:\d+\s+)?"(.*)"\s+'
            r"(\[.*?\]|\S+)\s+\S+\s+(\d+)\s+(\d+)\s+(\S+)"
            r"(?:\s+(\d+))?(?:\s+(\S+))?\s*",
            stripped,
        )
        if match:
            player = PlayerInfo(
                userid=match.group(1),
                name=match.group(2),
                uniqueid=match.group(3),
                ping=match.group(4),
                loss=match.group(5),
                state=match.group(6),
                rate=match.group(7) or "",
                address=match.group(8) or "",
            )
            status.players.append(player)
            continue

    return status


def format_status_message(status: ServerStatus, server_name: str) -> str:
    """Format a ServerStatus into a human-readable QQ message.

    Args:
        status: Parsed server status.
        server_name: Display name of the server.

    Returns:
        Formatted text message.
    """
    lines = [
        "=== L4D2 服务器状态 ===",
        f"服务器: {server_name}",
        f"主机名: {status.hostname or '未知'}",
        f"版本: {status.version or '未知'}",
        f"地图: {status.map or '未知'}",
        f"玩家: {status.player_count}/{status.max_players}",
    ]

    if status.players:
        lines.append("")
        lines.append("--- 在线玩家 ---")
        for p in status.players:
            lines.append(
                f"  [{p.userid}] {p.name} | ping:{p.ping}ms loss:{p.loss}% | {p.state}"
            )
    else:
        lines.append("")
        lines.append("当前无玩家在线")

    return "\n".join(lines)


def format_server_list(servers: list[dict], running_set: set[str]) -> str:
    """Format the list of configured servers into a readable message.

    Args:
        servers: List of raw server configuration dicts.
        running_set: Set of server names that are currently running.

    Returns:
        Formatted text message listing all servers.
    """
    if not servers:
        return "未配置任何 L4D2 服务器，请在插件配置中添加服务器。"

    lines = ["=== 已配置的 L4D2 服务器 ==="]
    for idx, srv in enumerate(servers, 1):
        name = srv.get("name", f"server_{idx}")
        host = srv.get("host", "127.0.0.1")
        port = srv.get("port", 27015)
        state = "运行中" if name in running_set else "已停止"
        lines.append(f"{idx}. {name} | {host}:{port} | {state}")

    lines.append("")
    lines.append("使用 /l4d2 status <服务器名> 查看详细状态")
    return "\n".join(lines)


def sanitize_rcon_arg(text: str) -> str:
    """Sanitize a string for safe use as an RCON command argument.

    Removes newlines, semicolons, and quotes that could be used for
    command injection in the RCON console.

    Args:
        text: Raw input string.

    Returns:
        Sanitized string safe for RCON arguments.
    """
    # Remove characters that could break out of the RCON command context
    sanitized = re.sub(r"[\r\n;\"']", "", text)
    return sanitized.strip()
