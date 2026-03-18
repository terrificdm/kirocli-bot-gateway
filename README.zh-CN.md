# KiroCLI Bot Gateway

Multi-platform bot gateway for Kiro CLI via ACP protocol.

> **选哪个项目？**
> - **本项目**（`kirocli-bot-gateway`）：多平台支持（飞书 + Discord + 更多）。如果需要多平台或未来可能扩展，推荐用这个。
> - [`feishu-kirocli-bot`](https://github.com/terrificdm/feishu-kirocli-bot)：仅支持飞书，轻量简单。如果只用飞书可以选这个。

## 支持的平台

| 平台 | 状态 | 说明 |
|------|------|------|
| 飞书 | ✅ 可用 | 群聊（@机器人）和私聊 |
| Discord | ✅ 可用 | 服务器频道（@机器人）和私聊 |

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                          Gateway                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │    飞书     │  │   Discord   │  │   (更多)    │   适配器     │
│  │   Adapter   │  │   Adapter   │  │             │              │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘              │
│         │                │                │                      │
│         └────────────────┼────────────────┘                      │
│                          ▼                                       │
│              ┌───────────────────────┐                           │
│              │      平台路由器        │                           │
│              └───────────┬───────────┘                           │
│                          │                                       │
│         ┌────────────────┼────────────────┐                      │
│         ▼                ▼                ▼                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │  kiro-cli   │  │  kiro-cli   │  │  kiro-cli   │  每平台独立   │
│  │   (飞书)    │  │  (discord)  │  │   (...)     │  实例        │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

## 功能特性

- **🔌 多平台支持**：一个网关服务多个聊天平台
- **🔒 平台隔离**：每个平台独立的 Kiro CLI 实例
- **📁 灵活的工作空间模式**：`per_chat`（用户隔离）或 `fixed`（共享项目）
- **🔐 交互式权限审批**：敏感操作需用户确认（y/n/t）
- **⚡ 按需启动**：仅在收到消息时启动 Kiro CLI
- **⏱️ 空闲自动关闭**：可配置的空闲超时
- **🖼️ 图片支持**：发送图片进行视觉分析（JPEG、PNG、GIF、WebP），自动检测真实格式
- **🛑 取消操作**：发送 "cancel" 中断当前操作
- **🔧 MCP 和 Skills 支持**：全局或项目级配置

## 工作空间模式（重要！）

这是最重要的配置，请仔细理解：

### `per_chat` 模式（默认，推荐多用户场景）

```
用户 A ──→ 会话 A ──→ /workspace/chat_id_A/
用户 B ──→ 会话 B ──→ /workspace/chat_id_B/
用户 C ──→ 会话 C ──→ /workspace/chat_id_C/
```

- 每个用户获得**独立的子目录**
- 用户之间无法看到或修改彼此的文件
- Kiro CLI 加载**全局** `~/.kiro/` 配置
- 适用于：公开机器人、多用户场景

### `fixed` 模式（推荐项目协作场景）

```
用户 A ──→ 会话 A ──┐
用户 B ──→ 会话 B ──┼──→ /path/to/project/
用户 C ──→ 会话 C ──┘
```

- 所有用户共享**同一个目录**
- Kiro CLI 加载**项目级** `.kiro/` 配置
- 适用于：团队协作、特定代码库

### MCP 和 Skills 配置位置

| 模式 | 配置位置 | 使用场景 |
|------|----------|----------|
| `per_chat` | `~/.kiro/settings/mcp.json`<br>`~/.kiro/skills/` | 所有用户共享的工具 |
| `fixed` | `{项目}/.kiro/settings/mcp.json`<br>`{项目}/.kiro/skills/` | 项目专用工具 |

### 按平台覆盖配置

不同平台可以使用不同模式：

```bash
# 全局默认
KIRO_WORKSPACE_MODE=per_chat

# 针对特定平台覆盖
FEISHU_WORKSPACE_MODE=per_chat   # 公开飞书机器人 - 隔离用户
DISCORD_WORKSPACE_MODE=fixed     # 团队 Discord - 共享项目
```

## 前置要求

- Python 3.11+
- [kiro-cli](https://kiro.dev/docs/cli/) 已安装并登录（`kiro-cli auth login`）
- 各平台的机器人凭证

## 安装

```bash
cd kirocli-bot-gateway
pip install -e .
```

## 配置

```bash
cp .env.example .env
# 编辑 .env 填入你的配置
```

详细配置选项请查看 `.env.example` 文件中的注释说明。

## 平台配置

### 飞书

1. 在[飞书开放平台](https://open.feishu.cn/app)创建企业自建应用
   - 点击**创建企业自建应用**
   - 填写应用名称和描述

2. 获取凭证：在**凭证与基础信息**中，复制 **App ID**（格式：`cli_xxx`）和 **App Secret** 到 `.env` 文件

3. 添加"机器人"能力：在**应用能力** > **机器人**中启用机器人 — `.env` 中的 `FEISHU_BOT_NAME` 必须与飞书中机器人的显示名称一致（通常与应用名称相同）

4. 配置权限（可在飞书开放平台权限页面批量导入）：
   - `im:message` - 读写消息（基础权限）
   - `im:message:send_as_bot` - 以机器人身份发送消息
   - `im:message:readonly` - 读取消息历史
   - `im:message.group_at_msg:readonly` - 接收群 @消息
   - `im:message.p2p_msg:readonly` - 接收私聊消息
   - `im:chat.access_event.bot_p2p_chat:read` - 私聊事件
   - `im:chat.members:bot_access` - 机器人群成员访问
   - `im:resource` - 访问消息资源（图片、文件等）

   <details>
   <summary>批量导入 JSON</summary>

   ```json
   {
     "scopes": {
       "tenant": [
         "im:message",
         "im:message:send_as_bot",
         "im:message:readonly",
         "im:message.group_at_msg:readonly",
         "im:message.p2p_msg:readonly",
         "im:chat.access_event.bot_p2p_chat:read",
         "im:chat.members:bot_access",
         "im:resource"
       ],
       "user": []
     }
   }
   ```

   </details>

5. 先启动机器人（保存事件订阅需要先建立连接）：
   ```bash
   python main.py
   ```
   此时机器人只连接飞书 WebSocket，还不会收到消息，但下一步需要这个连接。

6. 事件订阅：在**事件订阅**中，选择**使用长连接接收事件**（WebSocket）— 无需公网 Webhook URL
   - 添加事件：`im.message.receive_v1`

7. 发布应用：在**版本管理与发布**中，创建版本并发布
   - 企业自建应用通常自动审批
   - 权限变更需要发布新版本才能生效

> ⚠️ **外部群限制**：受限于飞书自身的管控，默认情况下，群聊模式的应用机器人**只能**添加到飞书企业内部群，更多情况请参考飞书文档。

### Discord

1. 在 [Discord 开发者门户](https://discord.com/developers/applications) 创建应用
   - 点击 **New Application** 并命名

2. 创建机器人：
   - 进入 **Bot** 标签页
   - 点击 **Add Bot**（如果还没有）
   - 在 **Privileged Gateway Intents** 下启用：
     - **MESSAGE CONTENT INTENT**（必须，用于读取消息内容）
     - **SERVER MEMBERS INTENT**（推荐，用于成员查找和白名单匹配）
   - 复制 **Token** 到 `.env` 文件的 `DISCORD_BOT_TOKEN`

3. 生成邀请链接：
   - 进入 **OAuth2** > **URL Generator**
   - 选择 scopes：`bot`、`applications.commands`
   - 选择 bot permissions：
     - View Channels
     - Send Messages
     - Send Messages in Threads
     - Embed Links
     - Attach Files
     - Read Message History
     - Add Reactions
   - 复制生成的 URL，打开它邀请机器人到你的服务器

4. 配置 `.env`：
   ```bash
   DISCORD_ENABLED=true
   DISCORD_BOT_TOKEN=your_token_here
   DISCORD_GUILD_ID=你的服务器ID        # 右键服务器 → 复制 ID
   DISCORD_ADMIN_USER_ID=你的用户ID     # 右键自己 → 复制 ID
   DISCORD_REQUIRE_MENTION=true          # 是否需要 @
   DISCORD_SLASH_COMMANDS=true           # 启用 /help, /agent, /model
   ```

   > **大多数用户到这里就够了！** 机器人会允许你私聊，并在你的服务器中响应。
   > 不需要额外的配置文件。

5. **高级：细粒度访问控制**（可选）：
   
   如果需要按服务器、频道、用户分别控制，创建 `discord_policy.json`：
   ```bash
   cp discord_policy.example.json discord_policy.json
   # 编辑 discord_policy.json，填入你的 ID
   ```

   当 `discord_policy.json` 存在时，它会**覆盖**上面的环境变量设置。

   示例策略：
   ```json
   {
     "dm": {
       "enabled": true,
       "policy": "allowlist",
       "allowFrom": ["你的用户ID"]
     },
     "groupPolicy": "allowlist",
     "guilds": {
       "*": {
         "requireMention": true
       },
       "你的服务器ID": {
         "requireMention": false,
         "users": ["你的用户ID"],
         "channels": {
           "*": { "allow": true },
           "特定频道ID": {
             "allow": true,
             "requireMention": true,
             "users": ["用户ID_1", "用户ID_2"]
           }
         }
       }
     },
     "allowBots": false
   }
   ```

   **策略选项说明：**
   - `dm.enabled`：启用/禁用私聊（默认：true）
   - `dm.policy`：`"allowlist"`（仅白名单用户）| `"open"`（任何人）| `"disabled"`（禁用）
   - `dm.allowFrom`：允许私聊的用户 ID 列表
   - `groupPolicy`：`"allowlist"`（仅白名单服务器/频道）| `"open"` | `"disabled"`
   - `guilds.<id>.users`：每个服务器的用户白名单（空 = 任何人）
   - `guilds.<id>.channels.<id>.allow`：允许特定频道
   - `guilds.<id>.channels.<id>.requireMention`：频道级 @ 覆盖
   - `guilds.<id>.channels.<id>.users`：频道级用户白名单
   - `guilds.<id>.requireMention`：是否需要 @（默认：true）
   - `guilds."*"`：未列出服务器的默认设置
   - `allowBots`：是否响应其他机器人（默认：false）

   **如何获取 ID：**
   - 启用开发者模式：Discord 设置 → 高级 → 开发者模式
   - 右键点击用户/服务器/频道 → 复制 ID

   **访问控制优先级：**
   1. `discord_policy.json`（如果存在）— 完全控制
   2. `DISCORD_ADMIN_USER_ID`（如果设置）— 简单白名单
   3. 都没有 — 私聊禁用，服务器开放但需要 @

6. 启动网关：
   ```bash
   python main.py
   ```

**使用方式：**
- **在服务器频道**：@机器人 进行交互（除非设置 `requireMention: false`）
- **在私聊**：直接发送消息（如果策略允许）

## 运行

```bash
python main.py
```

### 作为 systemd 服务运行（可选）

支持崩溃自动重启和开机自启：

```bash
# 复制并编辑 service 文件，修改为你的实际路径
cp kiro-gateway.service.example kiro-gateway.service
# 编辑 kiro-gateway.service，填入你的路径
sudo cp kiro-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kiro-gateway
sudo systemctl start kiro-gateway

# 查看状态 / 日志
sudo systemctl status kiro-gateway
journalctl -u kiro-gateway -f
```

> **⚠️ 注意：** systemd 不会继承你的 shell 环境变量。如果 kiro-cli 或 MCP server
> （如基于 npx 的）报 "No such file or directory"，需要编辑 `kiro-gateway.service`
> 中的 `Environment=PATH=...`，加入 `kiro-cli`、`npx` 等工具的安装路径
> （如 `~/.local/bin`、nvm 的 `bin` 目录）。

## 使用方法

### 图片支持

发送图片让 Kiro 分析——截图、架构图、报错信息等。

- **支持格式**：JPEG、PNG、GIF、WebP
- **自动格式检测**：网关从图片数据自动检测真实格式，修正平台可能误报的 MIME 类型
- **图片持久化**：图片保存到工作空间，Kiro 在后续对话轮次中可以重新读取

### 触发方式

| 平台 | 触发方式 |
|------|----------|
| 飞书群聊 | @机器人 + 消息 |
| 飞书私聊 | 直接发送消息 |
| Discord 服务器 | @机器人 + 消息 |
| Discord 私聊 | 直接发送消息 |

### 斜杠命令

| 命令 | 说明 |
|------|------|
| `/agent` | 列出可用的 Agent |
| `/agent <名称>` | 切换 Agent |
| `/model` | 列出可用的模型 |
| `/model <名称>` | 切换模型 |
| `/help` | 显示帮助 |

### 其他命令

| 命令 | 说明 |
|------|------|
| `cancel` / `stop` | 取消当前操作 |

### 权限审批

当 Kiro 需要执行敏感操作时：

```
🔐 Kiro 请求权限：
📋 创建文件：hello.txt
回复：y(允许) / n(拒绝) / t(信任)
⏱️ 60秒后自动拒绝
```

- **y** / yes / ok - 允许一次
- **n** / no - 拒绝
- **t** / trust / always - 本会话始终允许此类操作

## 图标说明

| 图标 | 含义 |
|------|------|
| 📄 | 读取文件 |
| 📝 | 编辑文件 |
| ⚡ | 执行命令 |
| 🔧 | 其他工具 |
| ✅ | 成功 |
| ❌ | 失败 |
| ⏳ | 进行中 |
| 🚫 | 已拒绝 |
| 🔐 | 权限请求 |

## 项目结构

```
kirocli-bot-gateway/
├── main.py                        # 入口
├── gateway.py                     # 核心网关逻辑
├── config.py                      # 配置管理
├── acp_client.py                  # ACP 协议客户端
├── .env.example                   # 环境配置模板（复制为 .env）
├── discord_policy.json            # Discord 访问策略（可选，覆盖环境变量）
├── discord_policy.example.json    # Discord 策略示例（复制后编辑）
├── pyproject.toml                 # Python 包配置
├── kiro-gateway.service.example    # systemd 服务模板（复制后编辑）
└── adapters/
    ├── __init__.py                # 包导出
    ├── base.py                    # ChatAdapter 接口
    ├── feishu.py                  # 飞书实现
    └── discord.py                 # Discord 实现
```

## 添加新平台

1. 创建 `adapters/yourplatform.py`
2. 实现 `adapters/base.py` 中的 `ChatAdapter` 接口
3. 在 `config.py` 中添加配置
4. 在 `main.py` 中注册适配器
