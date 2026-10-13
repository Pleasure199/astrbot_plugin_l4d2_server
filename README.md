# astrbot_plugin_l4d2_server

AstrBot 插件 — 通过 QQ 机器人远程管理求生之路 2（Left 4 Dead 2）专用服务器。

基于 AstrBot Star 插件系统开发，适配 OneBot v11（aiocqhttp）协议，支持服务器启停、状态查询、玩家管理等功能。

## 功能特性

- **服务器进程控制**：启动、停止、重启 srcds 服务端进程，支持 Windows / Linux 跨平台
- **状态查询**：在线状态、玩家列表、当前地图、延迟等实时信息
- **服务器管理**：切换地图、修改难度、踢人、全局广播
- **权限控制**：管理员 QQ 号白名单，查询类命令可配置公开访问
- **多服务器支持**：同时管理多台 L4D2 服务器实例
- **配置热加载**：WebUI 修改配置后即时生效
- **优雅停机**：优先通过 RCON 发送 quit 指令，失败后自动降级为强制终止

## 安装

1. 将本插件文件夹放入 AstrBot 的 `data/plugins/` 目录
2. 安装 Python 依赖：
   ```bash
   pip install python-valve psutil
   ```
3. 在 AstrBot WebUI 中启用插件
4. 配置服务器列表和管理员 QQ 号

## 配置说明

在 AstrBot WebUI 的插件配置页面中设置以下项：

### servers（服务器列表）

数组类型，每个元素为一个服务器配置对象：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| name | string | — | 服务器唯一标识名，用于命令中指定 |
| host | string | 127.0.0.1 | 服务器 IP 地址 |
| port | int | 27015 | 游戏端口 |
| rcon_port | int | 27015 | RCON 端口（通常与游戏端口相同） |
| rcon_password | string | — | RCON 密码 |
| srcds_path | string | — | srcds.exe（Windows）或 srcds_run（Linux）的完整路径 |
| start_params | string | — | 额外启动参数，如 `-tickrate 100` |
| max_players | int | 8 | 最大玩家数 |
| default_map | string | c2m1_highway | 启动时加载的地图 |

示例配置：

```json
[
  {
    "name": "my_server",
    "host": "127.0.0.1",
    "port": 27015,
    "rcon_port": 27015,
    "rcon_password": "your_rcon_password",
    "srcds_path": "C:\\l4d2\\srcds.exe",
    "start_params": "",
    "max_players": 8,
    "default_map": "c2m1_highway"
  }
]
```

### admins（管理员列表）

字符串数组，填写管理员的 QQ 号。只有列表中的用户可以执行启动/停止/重启/踢人/切图/改难度等高危操作。

### allow_public_query（允许公开查询）

布尔值，默认 `true`。开启后所有群成员可使用 `list`、`status`、`players` 等查询命令；关闭后仅管理员可查询。

## 指令列表

### 查询命令（所有人可用，取决于配置）

| 指令 | 说明 |
|------|------|
| `/l4d2 list` | 列出所有已配置服务器及运行状态 |
| `/l4d2 status [服务器名]` | 查询服务器详细状态（玩家、地图、延迟等） |
| `/l4d2 players [服务器名]` | 查看当前在线玩家列表 |
| `/l4d2 help` | 显示帮助信息 |

### 管理命令（仅管理员）

| 指令 | 说明 |
|------|------|
| `/l4d2 start [服务器名]` | 启动服务器 |
| `/l4d2 stop [服务器名]` | 停止服务器（优先优雅停机） |
| `/l4d2 restart [服务器名]` | 重启服务器 |
| `/l4d2 say <内容>` | 向服务器发送全局广播 |
| `/l4d2 kick <玩家名>` | 踢出指定玩家 |
| `/l4d2 map <地图名>` | 切换地图（如 c2m1_highway） |
| `/l4d2 diff <难度>` | 修改难度：easy / normal / hard / expert |

> **提示**：不指定服务器名时，默认操作列表中的第一台服务器。`say`、`kick`、`map`、`diff` 命令始终操作默认服务器。

## L4D2 服务端前置配置

在服务器的 `server.cfg` 中必须设置：

```cfg
rcon_password "你的密码"
sv_lan 0
```

确保 RCON 端口（默认 27015）对 AstrBot 所在机器可达。

## 安全注意事项

- RCON 密码为明文传输，建议仅在内网使用或配置 IP 白名单
- 管理员 QQ 号请仔细核对，避免授权给非信任用户
- `srcds_path` 指向的可执行文件应确保只有受信任用户有写权限

## 依赖

- Python >= 3.10
- AstrBot >= 4.16.0
- [python-valve](https://github.com/serverstf/python-valve) >= 0.2.1
- psutil >= 5.9.0

## 项目结构

```
astrbot_plugin_l4d2_server/
├── main.py                  # 插件主入口，Star 类实现与指令分发
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # 配置项定义（WebUI 可视化编辑）
├── requirements.txt         # Python 依赖
├── README.md                # 使用说明
└── core/
    ├── __init__.py
    ├── models.py            # 数据模型（ServerConfig, PlayerInfo, ServerStatus）
    ├── utils.py             # 工具函数（状态解析、消息格式化、参数清洗）
    ├── rcon_client.py       # RCON 客户端封装（异步、自动重试）
    └── process_manager.py   # 进程管理器（启停、状态监控、跨平台）
```

## License

MIT
