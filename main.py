"""L4D2 dedicated server management plugin for AstrBot.

Provides QQ bot commands to start, stop, restart, query, and manage
Left 4 Dead 2 dedicated servers via RCON and process control.
"""

from __future__ import annotations

import logging
import time
from functools import partial

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .core.models import ServerConfig
from .core.process_manager import ProcessManager
from .core.rcon_client import RconClient, RconError
from .core.utils import (
    format_server_list,
    format_status_message,
    parse_status_output,
    sanitize_rcon_arg,
)

logger = logging.getLogger("astrbot")

# Valid L4D2 difficulty levels
VALID_DIFFICULTIES = {"easy", "normal", "hard", "expert"}

# KV store key for saved start presets
KV_PRESETS_KEY = "saved_start_presets"


class L4D2ServerManager(Star):
    """L4D2 服务器远程管理插件。

    通过 QQ 指令远程控制求生之路 2 专用服务器的启停、状态查询、
    玩家管理等功能。支持多服务器实例、管理员权限控制和配置热加载。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """Initialize the L4D2 server manager plugin.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration dict (AstrBotConfig instance).
        """
        super().__init__(context)
        self.config = config if config is not None else {}
        self.process_manager = ProcessManager(self._parse_server_configs())

    def _parse_server_configs(self) -> list[ServerConfig]:
        """Parse server configurations from the plugin config dict.

        Returns:
            List of valid ServerConfig objects (those with a non-empty name).
        """
        raw_servers = self.config.get("servers", [])
        configs = []
        for srv in raw_servers:
            if isinstance(srv, dict) and str(srv.get("name", "")).strip():
                configs.append(ServerConfig.from_dict(srv))
        return configs

    def _refresh_process_manager(self) -> None:
        """Refresh the process manager's server configs from the live config.

        Called at the start of each command to support WebUI config hot-reload.
        Running processes are preserved; only the configuration metadata updates.
        """
        configs = self._parse_server_configs()
        self.process_manager.servers = {c.name: c for c in configs}

    def _get_rcon_client(self, server_name: str) -> RconClient | None:
        """Create an RCON client for the given server.

        Args:
            server_name: Name of the server.

        Returns:
            RconClient instance, or None if the server is not configured.
        """
        config = self.process_manager.get_server(server_name)
        if config is None:
            return None
        connect = None
        if config.ssh_enabled and config.rcon_via_ssh:
            # The RCON port is often not reachable from the machine running
            # AstrBot, so a server managed over SSH is reached through a tunnel
            # on that same SSH connection. The game server runs on the SSH host
            # itself, whose loopback address is tried because a host firewall
            # often blocks its own public address.
            ssh = self.process_manager.get_ssh_client(server_name)
            if ssh is not None:
                connect = partial(
                    ssh.open_rcon_tunnel,
                    config.rcon_port,
                    host=config.rcon_tunnel_host,
                )
        return RconClient(
            host=config.host,
            port=config.rcon_port,
            password=config.rcon_password,
            connect=connect,
        )

    def _resolve_server(self, server_name: str) -> tuple[str | None, str | None]:
        """Resolve a server name, defaulting to the first configured server.

        Args:
            server_name: User-provided server name (may be empty for default).

        Returns:
            A tuple of (resolved_server_name, error_message).
            If error_message is not None, resolution failed.
        """
        self._refresh_process_manager()
        names = self.process_manager.get_all_server_names()
        if not names:
            return None, "未配置任何 L4D2 服务器，请在插件配置中添加服务器。"
        if not server_name:
            return names[0], None
        if server_name not in names:
            return (
                None,
                f"服务器 '{server_name}' 不存在。\n可用服务器: {', '.join(names)}",
            )
        return server_name, None

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """Check if the message sender is in the admin whitelist.

        Args:
            event: The message event.

        Returns:
            True if the sender is an admin, False otherwise.
        """
        sender_id = str(event.get_sender_id())
        admins = [str(a) for a in self.config.get("admins", [])]
        return sender_id in admins

    def _check_admin(self, event: AstrMessageEvent) -> str | None:
        """Verify admin permission for high-risk operations.

        Args:
            event: The message event.

        Returns:
            An error message if not authorized, None if authorized.
        """
        if not self._is_admin(event):
            return "权限不足：仅管理员可执行此操作。"
        return None

    def _check_query_permission(self, event: AstrMessageEvent) -> str | None:
        """Verify permission for query operations.

        Query commands are available to everyone if allow_public_query is True,
        otherwise restricted to admins.

        Args:
            event: The message event.

        Returns:
            An error message if not authorized, None if authorized.
        """
        if self._is_admin(event):
            return None
        if not self.config.get("allow_public_query", True):
            return "查询功能已被管理员限制，仅管理员可使用。"
        return None

    # --- Saved start presets ---

    async def _load_presets(self) -> dict:
        """Load all saved start presets from the KV store.

        Returns:
            Dict mapping preset names to preset data dicts.
        """
        return await self.get_kv_data(KV_PRESETS_KEY, {}) or {}

    async def _save_presets(self, presets: dict) -> None:
        """Persist the presets dict to the KV store.

        Args:
            presets: The complete presets dict to save.
        """
        await self.put_kv_data(KV_PRESETS_KEY, presets)

    async def _get_preset(self, name: str) -> dict | None:
        """Get a single saved preset by name.

        Args:
            name: Preset name.

        Returns:
            Preset data dict, or None if not found.
        """
        presets = await self._load_presets()
        return presets.get(name)

    def _resolve_preset_server(
        self, preset: dict, user_server: str
    ) -> tuple[str | None, str | None]:
        """Resolve which server a preset should operate on.

        Priority: user-specified server > preset's server > default server.

        Args:
            preset: The preset data dict.
            user_server: Server name explicitly provided by the user (may be empty).

        Returns:
            A tuple of (resolved_server_name, error_message).
        """
        self._refresh_process_manager()
        names = self.process_manager.get_all_server_names()
        if not names:
            return None, "未配置任何 L4D2 服务器。"

        candidate = user_server or preset.get("server", "")
        if not candidate:
            return names[0], None
        if candidate not in names:
            return None, f"服务器 '{candidate}' 不存在。可用: {', '.join(names)}"
        return candidate, None

    async def initialize(self) -> None:
        """Plugin initialization hook.

        Reloads the server configuration and re-detects remote server states.
        """
        self._refresh_process_manager()
        count = len(self.process_manager.get_all_server_names())
        logger.info(
            "L4D2ServerManager initialized with %d configured server(s).", count
        )
        await self.process_manager.refresh_ssh_states()

    async def terminate(self) -> None:
        """Plugin termination hook.

        Intentionally leaves the servers running: AstrBot reloads a plugin
        whenever its configuration is saved, so stopping the servers here would
        shut them down on every config change. Use /l4d2 stop instead.
        """
        logger.info("L4D2ServerManager unloading; running servers are left untouched.")

    # --- Command group definition ---

    @filter.command_group("l4d2")
    def l4d2():
        """L4D2 服务器管理指令组"""
        pass

    # --- Query commands (public if allowed) ---

    @l4d2.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出所有已配置的 L4D2 服务器及其运行状态"""
        error = self._check_query_permission(event)
        if error:
            yield event.plain_result(error)
            return

        self._refresh_process_manager()
        raw_servers = self.config.get("servers", [])
        running = self.process_manager.get_running_servers()
        yield event.plain_result(format_server_list(raw_servers, running))

    @l4d2.command("status")
    async def cmd_status(self, event: AstrMessageEvent, server_name: str = ""):
        """查询服务器状态（在线状态、玩家列表、地图等）"""
        error = self._check_query_permission(event)
        if error:
            yield event.plain_result(error)
            return

        name, err = self._resolve_server(server_name)
        if err:
            yield event.plain_result(err)
            return

        # Check process status first
        if self.process_manager.is_running(name):
            rcon = self._get_rcon_client(name)
            if rcon is None:
                yield event.plain_result(
                    f"服务器 '{name}' 配置缺失，无法创建 RCON 客户端。"
                )
                return
            try:
                raw = await rcon.status()
                status = parse_status_output(raw)
                yield event.plain_result(format_status_message(status, name))
            except RconError as e:
                yield event.plain_result(
                    f"服务器 '{name}' 进程运行中，但 RCON 连接失败：\n{e}\n"
                    f"服务器可能尚未完全启动，请稍后重试。"
                )
        else:
            yield event.plain_result(
                f"服务器 '{name}' 当前未运行。使用 /l4d2 start {name} 启动。"
            )

    @l4d2.command("players")
    async def cmd_players(self, event: AstrMessageEvent, server_name: str = ""):
        """查看当前在线玩家列表"""
        error = self._check_query_permission(event)
        if error:
            yield event.plain_result(error)
            return

        name, err = self._resolve_server(server_name)
        if err:
            yield event.plain_result(err)
            return

        if not self.process_manager.is_running(name):
            yield event.plain_result(f"服务器 '{name}' 当前未运行。")
            return

        rcon = self._get_rcon_client(name)
        if rcon is None:
            yield event.plain_result(f"服务器 '{name}' 配置缺失。")
            return

        try:
            raw = await rcon.status()
            status = parse_status_output(raw)
            if not status.players:
                yield event.plain_result(f"服务器 '{name}' 当前无玩家在线。")
            else:
                lines = [f"=== {name} 在线玩家 ({len(status.players)}人) ==="]
                for p in status.players:
                    lines.append(
                        f"  [{p.userid}] {p.name}\n"
                        f"    延迟: {p.ping}ms | 丢包: {p.loss}% | 状态: {p.state}"
                    )
                yield event.plain_result("\n".join(lines))
        except RconError as e:
            yield event.plain_result(f"RCON 连接失败：{e}")

    # --- Admin commands: process control ---

    @l4d2.command("start")
    async def cmd_start(self, event: AstrMessageEvent, server_name: str = ""):
        """启动 L4D2 服务器（仅管理员）"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        name, err = self._resolve_server(server_name)
        if err:
            yield event.plain_result(err)
            return

        yield event.plain_result(f"正在启动服务器 '{name}'...")
        success, msg = await self.process_manager.start(name)
        yield event.plain_result(msg)

    @l4d2.command("stop")
    async def cmd_stop(self, event: AstrMessageEvent, server_name: str = ""):
        """停止 L4D2 服务器（仅管理员）"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        name, err = self._resolve_server(server_name)
        if err:
            yield event.plain_result(err)
            return

        rcon = self._get_rcon_client(name)
        yield event.plain_result(f"正在停止服务器 '{name}'...")
        success, msg = await self.process_manager.stop(name, rcon_client=rcon)
        yield event.plain_result(msg)

    @l4d2.command("restart")
    async def cmd_restart(self, event: AstrMessageEvent, server_name: str = ""):
        """重启 L4D2 服务器（仅管理员）"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        name, err = self._resolve_server(server_name)
        if err:
            yield event.plain_result(err)
            return

        rcon = self._get_rcon_client(name)
        yield event.plain_result(f"正在重启服务器 '{name}'...")
        success, msg = await self.process_manager.restart(name, rcon_client=rcon)
        yield event.plain_result(msg)

    # --- Saved start preset commands ---

    @l4d2.command("save")
    async def cmd_save(self, event: AstrMessageEvent, name: str, rest: GreedyStr):
        """保存一个启动预设（仅管理员）。用法：/l4d2 save <名称> [服务器名] <启动参数...>"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        safe_name = sanitize_rcon_arg(name)
        if not safe_name:
            yield event.plain_result("预设名称不能为空。")
            return

        self._refresh_process_manager()
        server_names = set(self.process_manager.get_all_server_names())

        # Parse rest: first token may be a server name, remaining are params
        tokens = rest.split()
        preset_server = ""
        preset_params = rest
        if tokens and tokens[0] in server_names:
            preset_server = tokens[0]
            preset_params = " ".join(tokens[1:])

        if not preset_params.strip():
            yield event.plain_result(
                "启动参数不能为空。\n用法：/l4d2 save <名称> [服务器名] <启动参数...>"
            )
            return

        presets = await self._load_presets()
        is_update = safe_name in presets
        presets[safe_name] = {
            "server": preset_server,
            "params": preset_params,
            "created_by": str(event.get_sender_id()),
            "created_at": int(time.time()),
        }
        await self._save_presets(presets)

        action = "更新" if is_update else "保存"
        server_info = f"服务器: {preset_server}" if preset_server else "服务器: 默认"
        yield event.plain_result(
            f"已{action}启动预设 '{safe_name}'。\n"
            f"{server_info}\n"
            f"参数: {preset_params}\n"
            f"使用 /l4d2 run {safe_name} 启动。"
        )

    @l4d2.command("saves")
    async def cmd_saves(self, event: AstrMessageEvent):
        """列出所有已保存的启动预设"""
        presets = await self._load_presets()
        if not presets:
            yield event.plain_result(
                "暂无保存的启动预设。\n"
                "使用 /l4d2 save <名称> [服务器名] <启动参数...> 保存。"
            )
            return

        lines = [f"=== 已保存的启动预设 ({len(presets)}个) ==="]
        for idx, (pname, pdata) in enumerate(presets.items(), 1):
            server = pdata.get("server", "") or "默认"
            params = pdata.get("params", "")
            created_at = pdata.get("created_at", 0)
            time_str = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at))
                if created_at
                else "未知"
            )
            lines.append(
                f"{idx}. {pname}\n"
                f"   服务器: {server} | 保存于: {time_str}\n"
                f"   参数: {params}"
            )
        lines.append("")
        lines.append("使用 /l4d2 run <预设名> 启动，/l4d2 unsave <预设名> 删除。")
        yield event.plain_result("\n".join(lines))

    @l4d2.command("unsave")
    async def cmd_unsave(self, event: AstrMessageEvent, name: str):
        """删除一个已保存的启动预设（仅管理员）"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        safe_name = sanitize_rcon_arg(name)
        presets = await self._load_presets()
        if safe_name not in presets:
            yield event.plain_result(f"预设 '{safe_name}' 不存在。")
            return

        del presets[safe_name]
        await self._save_presets(presets)
        yield event.plain_result(f"已删除启动预设 '{safe_name}'。")

    @l4d2.command("run")
    async def cmd_run(self, event: AstrMessageEvent, name: str, server_name: str = ""):
        """使用保存的启动预设启动服务器（仅管理员）"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        safe_name = sanitize_rcon_arg(name)
        preset = await self._get_preset(safe_name)
        if preset is None:
            yield event.plain_result(
                f"预设 '{safe_name}' 不存在。\n使用 /l4d2 saves 查看所有预设。"
            )
            return

        target_server, err = self._resolve_preset_server(preset, server_name)
        if err:
            yield event.plain_result(err)
            return

        extra_params = preset.get("params", "")
        yield event.plain_result(
            f"使用预设 '{safe_name}' 启动服务器 '{target_server}'...\n"
            f"额外参数: {extra_params}"
        )
        success, msg = await self.process_manager.start(
            target_server, extra_params=extra_params
        )
        yield event.plain_result(msg)

    # --- Admin commands: server management via RCON ---

    @l4d2.command("say")
    async def cmd_say(self, event: AstrMessageEvent, content: GreedyStr):
        """向服务器发送全局广播消息（仅管理员）"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        name, err = self._resolve_server("")
        if err:
            yield event.plain_result(err)
            return

        if not self.process_manager.is_running(name):
            yield event.plain_result(f"服务器 '{name}' 当前未运行。")
            return

        rcon = self._get_rcon_client(name)
        if rcon is None:
            yield event.plain_result(f"服务器 '{name}' 配置缺失。")
            return

        safe_content = sanitize_rcon_arg(content)
        if not safe_content:
            yield event.plain_result("广播内容不能为空。")
            return

        try:
            await rcon.say(safe_content)
            yield event.plain_result(f"已向服务器 '{name}' 发送广播：{safe_content}")
        except RconError as e:
            yield event.plain_result(f"发送广播失败：{e}")

    @l4d2.command("kick")
    async def cmd_kick(self, event: AstrMessageEvent, player_name: GreedyStr):
        """踢出指定玩家（仅管理员）"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        name, err = self._resolve_server("")
        if err:
            yield event.plain_result(err)
            return

        if not self.process_manager.is_running(name):
            yield event.plain_result(f"服务器 '{name}' 当前未运行。")
            return

        rcon = self._get_rcon_client(name)
        if rcon is None:
            yield event.plain_result(f"服务器 '{name}' 配置缺失。")
            return

        safe_player = sanitize_rcon_arg(player_name)
        if not safe_player:
            yield event.plain_result("玩家名不能为空。")
            return

        try:
            result = await rcon.kick(safe_player)
            yield event.plain_result(
                f"已尝试踢出玩家 '{safe_player}'。\n服务器响应：{result or '（无）'}"
            )
        except RconError as e:
            yield event.plain_result(f"踢出玩家失败：{e}")

    @l4d2.command("map")
    async def cmd_map(self, event: AstrMessageEvent, map_name: str):
        """切换服务器地图（仅管理员）"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        name, err = self._resolve_server("")
        if err:
            yield event.plain_result(err)
            return

        if not self.process_manager.is_running(name):
            yield event.plain_result(f"服务器 '{name}' 当前未运行。")
            return

        rcon = self._get_rcon_client(name)
        if rcon is None:
            yield event.plain_result(f"服务器 '{name}' 配置缺失。")
            return

        safe_map = sanitize_rcon_arg(map_name)
        if not safe_map:
            yield event.plain_result("地图名不能为空。")
            return

        try:
            await rcon.changelevel(safe_map)
            yield event.plain_result(f"已向服务器 '{name}' 发送切图指令：{safe_map}")
        except RconError as e:
            yield event.plain_result(f"切换地图失败：{e}")

    @l4d2.command("diff")
    async def cmd_diff(self, event: AstrMessageEvent, difficulty: str):
        """修改游戏难度（仅管理员），可选值：easy/normal/hard/expert"""
        error = self._check_admin(event)
        if error:
            yield event.plain_result(error)
            return

        diff_lower = difficulty.lower().strip()
        if diff_lower not in VALID_DIFFICULTIES:
            yield event.plain_result(
                f"无效难度 '{difficulty}'。\n"
                f"可选值：easy（简单）、normal（普通）、hard（困难）、expert（专家）"
            )
            return

        name, err = self._resolve_server("")
        if err:
            yield event.plain_result(err)
            return

        if not self.process_manager.is_running(name):
            yield event.plain_result(f"服务器 '{name}' 当前未运行。")
            return

        rcon = self._get_rcon_client(name)
        if rcon is None:
            yield event.plain_result(f"服务器 '{name}' 配置缺失。")
            return

        try:
            await rcon.set_difficulty(diff_lower)
            diff_names = {
                "easy": "简单",
                "normal": "普通",
                "hard": "困难",
                "expert": "专家",
            }
            yield event.plain_result(
                f"已将服务器 '{name}' 难度设置为 {diff_lower}（{diff_names[diff_lower]}）。"
            )
        except RconError as e:
            yield event.plain_result(f"修改难度失败：{e}")

    # --- Help ---

    @l4d2.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示 L4D2 插件帮助信息"""
        is_admin = self._is_admin(event)
        lines = [
            "=== L4D2 服务器管理插件 ===",
            "",
            "【查询命令】",
            "  /l4d2 list              列出所有服务器及状态",
            "  /l4d2 status [服务器名]  查询服务器详细状态",
            "  /l4d2 players [服务器名] 查看在线玩家",
            "  /l4d2 help              显示此帮助信息",
        ]
        if is_admin:
            lines.extend(
                [
                    "",
                    "【管理命令】（仅管理员）",
                    "  /l4d2 start [服务器名]   启动服务器",
                    "  /l4d2 stop [服务器名]    停止服务器",
                    "  /l4d2 restart [服务器名] 重启服务器",
                    "  /l4d2 say <内容>         全局广播消息",
                    "  /l4d2 kick <玩家名>      踢出玩家",
                    "  /l4d2 map <地图名>       切换地图",
                    "  /l4d2 diff <难度>        修改难度 (easy/normal/hard/expert)",
                    "",
                    "【启动预设】（仅管理员）",
                    "  /l4d2 save <名称> [服务器名] <参数...>  保存启动预设",
                    "  /l4d2 saves                               列出所有预设",
                    "  /l4d2 unsave <名称>                       删除预设",
                    "  /l4d2 run <名称> [服务器名]               使用预设启动",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "（管理命令仅管理员可见，如需使用请联系管理员。）",
                ]
            )
        lines.extend(
            [
                "",
                "提示：不指定服务器名时默认操作第一台服务器。",
            ]
        )
        yield event.plain_result("\n".join(lines))
