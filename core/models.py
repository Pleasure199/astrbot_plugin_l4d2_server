"""Data models for L4D2 server management."""

from __future__ import annotations

from dataclasses import dataclass, field


def _to_int(value, default: int) -> int:
    """Convert a raw configuration value to int.

    Args:
        value: Raw value from the plugin configuration, which may be an empty
            string or ``None`` when a template entry was left blank.
        default: Value returned when the input cannot be converted.

    Returns:
        The converted integer, or ``default``.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value, default: bool) -> bool:
    """Convert a raw configuration value to bool.

    Args:
        value: Raw value from the plugin configuration. Hand-written JSON may
            store booleans as strings, where ``bool("false")`` would be wrong.
        default: Value returned when the input is not recognised.

    Returns:
        The converted boolean, or ``default``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off", ""}:
            return False
    return default


@dataclass
class ServerConfig:
    """Configuration for a single L4D2 dedicated server.

    Attributes:
        name: Unique server identifier used in commands.
        host: Server IP address for RCON and game connections.
        port: Game port (default 27015).
        rcon_port: RCON port (usually same as game port).
        rcon_password: RCON password for remote console access.
        srcds_path: Filesystem path to srcds.exe (Windows) or srcds_run (Linux).
        start_params: Additional start parameters appended to the base command.
        max_players: Maximum number of players allowed on the server.
        default_map: Map to load on server start.
        ssh_enabled: Whether to manage the server via SSH (for remote servers).
        ssh_host: SSH server hostname or IP.
        ssh_port: SSH port (default 22).
        ssh_username: SSH login username.
        ssh_password: SSH login password (or passphrase for key).
        ssh_key_path: Path to SSH private key file (alternative to password).
        rcon_via_ssh: Whether RCON traffic is tunnelled through the SSH session.
        rcon_tunnel_host: Tunnel destination as seen from the SSH host; empty
            means the SSH host itself (127.0.0.1).
    """

    name: str
    host: str = "127.0.0.1"
    port: int = 27015
    rcon_port: int = 27015
    rcon_password: str = ""
    srcds_path: str = ""
    start_params: str = ""
    max_players: int = 8
    default_map: str = "c2m1_highway"
    ssh_enabled: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_username: str = ""
    ssh_password: str = ""
    ssh_key_path: str = ""
    rcon_via_ssh: bool = True
    rcon_tunnel_host: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> ServerConfig:
        """Create a ServerConfig from a raw dictionary, ignoring unknown keys.

        Args:
            data: Raw configuration dictionary.

        Returns:
            A populated ServerConfig instance.
        """
        return cls(
            name=str(data.get("name", "")).strip(),
            host=str(data.get("host", "127.0.0.1")).strip() or "127.0.0.1",
            port=_to_int(data.get("port"), 27015),
            rcon_port=_to_int(data.get("rcon_port"), 27015),
            rcon_password=str(data.get("rcon_password", "")),
            srcds_path=str(data.get("srcds_path", "")).strip(),
            start_params=str(data.get("start_params", "")),
            max_players=_to_int(data.get("max_players"), 8),
            default_map=str(data.get("default_map", "")).strip() or "c2m1_highway",
            ssh_enabled=_to_bool(data.get("ssh_enabled"), False),
            ssh_host=str(data.get("ssh_host", "")).strip(),
            ssh_port=_to_int(data.get("ssh_port"), 22),
            ssh_username=str(data.get("ssh_username", "")).strip(),
            ssh_password=str(data.get("ssh_password", "")),
            ssh_key_path=str(data.get("ssh_key_path", "")).strip(),
            rcon_via_ssh=_to_bool(data.get("rcon_via_ssh"), True),
            rcon_tunnel_host=str(data.get("rcon_tunnel_host", "")).strip(),
        )


@dataclass
class PlayerInfo:
    """Information about a player currently connected to the server.

    Attributes:
        name: Player display name.
        userid: Server-side user ID.
        uniqueid: Steam ID or other unique identifier.
        ping: Player ping in milliseconds.
        loss: Packet loss percentage.
        state: Connection state (active, connecting, etc.).
        rate: Player's rate setting.
        address: Player's IP address and port.
    """

    name: str = ""
    userid: str = ""
    uniqueid: str = ""
    ping: str = ""
    loss: str = ""
    state: str = ""
    rate: str = ""
    address: str = ""


@dataclass
class ServerStatus:
    """Parsed status information for an L4D2 server.

    Attributes:
        hostname: Server hostname.
        version: Server software version.
        map: Current map name.
        player_count: Number of players currently connected.
        max_players: Maximum allowed players.
        players: List of connected player details.
        raw: Raw status output from the server.
    """

    hostname: str = ""
    version: str = ""
    map: str = ""
    player_count: int = 0
    max_players: int = 0
    players: list[PlayerInfo] = field(default_factory=list)
    raw: str = ""
