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
- **启动预设**：保存常用启动参数为命名预设，一键调用（持久化存储）
- **优雅停机**：优先通过 RCON 发送 quit 指令，失败后自动降级为终止整个进程树
- **进程独立存活**：服务器以独立进程运行，重载/禁用插件不会影响正在运行的服务器

## 安装

1. 将本插件文件夹放入 AstrBot 的 `data/plugins/` 目录
2. 安装 Python 依赖：
   ```bash
   pip install psutil
   ```
3. 在 AstrBot WebUI 中启用插件
4. 配置服务器列表和管理员 QQ 号

## 配置说明

在 AstrBot WebUI 的插件配置页面中设置以下项：

### servers（服务器列表）

使用模板化列表编辑器，点击「添加」→「L4D2 专用服务器」即可创建一台服务器配置。每台服务器包含以下字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| name | string | — | 服务器标识名，用于指令中指定（如 `/l4d2 start 服务器名`） |
| host | string | 127.0.0.1 | 服务器 IP 地址（RCON 连接地址） |
| port | int | 27015 | 游戏端口 |
| rcon_port | int | 27015 | RCON 端口（通常与游戏端口相同） |
| rcon_password | string | — | RCON 密码（密码掩码显示，需与 server.cfg 一致） |
| srcds_path | string | — | 服务器目录路径，或 srcds.exe（Windows）/ srcds_run（Linux）的完整路径 |
| start_params | string | — | 额外启动参数，如 `-tickrate 100 +mp_gamemode versus` |
| max_players | int | 8 | 最大玩家数 |
| default_map | string | c2m1_highway | 启动时加载的地图 |
| ssh_enabled | bool | false | 启用 SSH 远程管理（AstrBot 与服务器不在同一台机器时开启） |
| ssh_host | string | — | SSH 主机地址（远程服务器 IP 或域名） |
| ssh_port | int | 22 | SSH 端口 |
| ssh_username | string | — | SSH 登录用户名 |
| ssh_password | string | — | SSH 密码（密码掩码显示） |
| ssh_key_path | string | — | SSH 私钥文件本地路径（可选，替代密码认证） |
| rcon_via_ssh | bool | true | RCON 是否经 SSH 通道连接（服务器未对外开放 RCON 端口时保持开启） |
| rcon_tunnel_host | string | — | RCON 隧道目标地址，留空表示远程主机本机（127.0.0.1） |

> 配置卡片标题会自动显示服务器的 `name` 字段，方便快速识别多台服务器。

> **启动方式优先级**：`srcds_path` 可填服务器目录或 srcds 可执行文件路径。若对应目录下存在 `start.sh`（Linux）或 `start.bat` / `start.sh`（Windows），插件将优先执行该脚本启动服务器（`start_params` 会作为参数传入脚本）；脚本不存在时才回退为直接调用 srcds 并拼接端口、地图等参数。启动成功后会在回复中注明使用的是哪种方式。

### SSH 远程管理（AstrBot 与服务器不在同一台机器）

当 AstrBot 运行在 Windows 桌面版，而 L4D2 服务器部署在远程 Linux 机器上时，启用 SSH 模式：

1. 在服务器配置中打开 `ssh_enabled`
2. 填写 `ssh_host`、`ssh_port`、`ssh_username`、`ssh_password`（或 `ssh_key_path`）
3. `srcds_path` 填写远程服务器上的目录路径（如 `/home/l4d2/Steam/l4d2server`）
4. 确保远程服务器的 SSH 端口对 AstrBot 所在机器开放

启用后：
- `/l4d2 start` 通过 SSH 在远程服务器上执行启动命令（优先使用 `start.sh`，使用 `setsid nohup` 后台运行）；启动时会把服务器的进程组记录到服务器目录下的 `.astrbot_l4d2.pid`
- `/l4d2 stop` 优先通过 RCON 发送 `quit`，失败则按该进程组结束服务器（启动脚本里的自动重启循环会一并终止），最后再按目录匹配兜底
- `/l4d2 status`、`players`、`say`、`kick`、`map`、`diff` 等 RCON 指令默认**经 SSH 通道转发**到服务器（见 `rcon_via_ssh`），因此即使远程主机的 RCON 端口没有对外开放也能正常使用

> **依赖**：SSH 模式需要 `paramiko` 库，已包含在 `requirements.txt` 中。

> **RCON 端口无法直连时**：这是很常见的情况（防火墙只放行了游戏端口 UDP，或 RCON 只在服务器本机监听）。此时保持 `rcon_via_ssh` 开启即可，插件会复用同一条 SSH 连接，把 RCON 流量转发到**远程主机自己的 `127.0.0.1:<rcon_port>`**（也就是说 `host` 字段在隧道模式下不再起作用，游戏服务器就在那台机器上，填 127.0.0.1 可以绕开主机自身防火墙对公网地址的拦截）。如果 RCON 密码正确、`rcon_port` 也填对了却依然失败，请检查服务器上是否真的有进程在监听：`ss -lntp | grep 27015`（注意 RCON 是 **TCP**，游戏端口是 UDP）。

> 隧道会自动选择可用的目标地址：先试远程主机的 `127.0.0.1`，失败时读取服务端**实际监听的地址**（`ss -lnt`）再试一次。因此 srcds 用 `-ip` 绑定内网地址（例如阿里云 VPC 的 `172.x.x.x`）时也能连上，无需手工配置。

> 只有在下面两种情况下才需要把 `rcon_via_ssh` 关掉改回直连：AstrBot 所在机器能直接连上 RCON 端口；或者远程主机禁止了 SSH 端口转发（`AllowTcpForwarding no`，此时隧道会报 `channel open failure`）。

> **报错 `ChannelException(2, 'Connect failed')` 时**：这表示远程主机在报错列出的那些地址上都连不上 RCON，通常就是**服务端没有在监听 RCON 端口**。按顺序排查：
> 1. `server.cfg` 里是否有 `rcon_password "密码"`，且修改后**重启过**服务端 —— Source 引擎只有在设置了 `rcon_password` 之后才会打开 RCON 监听；
> 2. 在服务器上执行 `ss -lntp | grep <rcon_port>`，确认该端口处于 LISTEN（RCON 是 **TCP**，很容易只放行了 UDP 的游戏端口）；
> 3. 服务端若跑在 Docker 里，需要发布 TCP 端口（例如 `-p 10721:27015/tcp`）；如果容器内端口与宿主机映射端口不同，把 `rcon_tunnel_host` 填成容器 IP、`rcon_port` 填容器内的端口。

### admins（管理员 QQ 号）

字符串列表，填写管理员的 QQ 号。只有列表中的用户可以执行启动/停止/重启/踢人/切图/改难度等高危操作。

### allow_public_query（允许公开查询）

布尔值，默认 `true`。开启后所有人可使用 `list`、`status`、`players` 等查询命令；关闭后仅管理员可查询。

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

### 启动预设（仅管理员）

可将常用的启动参数保存为命名预设，后续通过名称快速调用。预设数据持久化存储在插件 KV 存储中。

| 指令 | 说明 |
|------|------|
| `/l4d2 save <名称> [服务器名] <启动参数...>` | 保存一个启动预设 |
| `/l4d2 saves` | 列出所有已保存的启动预设 |
| `/l4d2 unsave <名称>` | 删除一个启动预设 |
| `/l4d2 run <名称> [服务器名]` | 使用保存的预设启动服务器 |

**示例：**
```
/l4d2 save versus srv1 +mp_gamemode versus +sv_cheats 0
/l4d2 run versus
/l4d2 save coop -tickrate 100 +mp_gamemode coop
/l4d2 saves
/l4d2 unsave coop
```

> 保存预设时，若第一个参数匹配已配置的服务器名，则绑定到该服务器；否则参数全部视为启动参数，运行时使用默认服务器。`run` 命令中显式指定的服务器名优先级最高。

> **提示**：不指定服务器名时，默认操作列表中的第一台服务器。`say`、`kick`、`map`、`diff` 命令始终操作默认服务器。

## 进程管理说明

- 服务器由插件启动后作为独立进程运行，即使重载或禁用插件也会继续运行，只有 `/l4d2 stop` 会将其停止。
- 停止服务器时，插件会终止整棵进程树：`start.sh` / `start.bat` 这类启动脚本只是外层包装，真正的 `srcds` 进程是它的子进程（本地模式按进程树终止，SSH 模式按进程组终止，因此启动脚本里的自动重启循环也会一起停掉）。
- 插件会在每次启动/停止/查询时按服务器目录重新识别正在运行的 `srcds` 进程，因此即使插件被重载，`/l4d2 status`、`/l4d2 stop` 依然可以正确工作。
- SSH 模式下停止服务器只会终止该服务器目录对应的 `srcds` 进程，同一台主机上的其他 L4D2 服务器不受影响。
- SSH 模式启动服务器时会在服务器目录写入 `.astrbot_l4d2.pid`（记录该服务器的进程组），停止成功后自动删除；这个文件只在本插件内使用。
- 若插件在远程主机上找不到属于该服务器的进程，会直接在回复中列出主机上现有的 `srcds` 进程（PID 与运行目录），便于确认 `srcds_path` 是否指向了实际运行目录。
- SSH 模式下 RCON 指令（查询、广播、踢人、切图等）默认经 SSH 通道转发，不要求远程主机对外开放 RCON 端口；`/l4d2 status` 能正常显示主机名、地图、玩家时即表示这条链路通畅。

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
- psutil >= 5.9.0
- paramiko >= 3.0.0（仅 SSH 远程管理模式需要）

> RCON 协议由插件自身实现（仅使用 Python 标准库），无需额外安装 `python-valve` 等第三方 RCON 库。

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
