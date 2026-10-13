"""Data models for L4D2 server management."""

from __future__ import annotations

from dataclasses import dataclass, field


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

    @classmethod
    def from_dict(cls, data: dict) -> "ServerConfig":
        """Create a ServerConfig from a raw dictionary, ignoring unknown keys.

        Args:
            data: Raw configuration dictionary.

        Returns:
            A populated ServerConfig instance.
        """
        return cls(
            name=str(data.get("name", "")),
            host=str(data.get("host", "127.0.0.1")),
            port=int(data.get("port", 27015)),
            rcon_port=int(data.get("rcon_port", 27015)),
            rcon_password=str(data.get("rcon_password", "")),
            srcds_path=str(data.get("srcds_path", "")),
            start_params=str(data.get("start_params", "")),
            max_players=int(data.get("max_players", 8)),
            default_map=str(data.get("default_map", "c2m1_highway")),
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
