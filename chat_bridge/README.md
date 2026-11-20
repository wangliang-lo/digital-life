# IM ⇄ 数字生命 中间件

把本机IM接进 `digital_life.py`：**文字、语音、图片**都能听懂，回复能带**表情包**；外加定时主动聊天，和一个给 DSH 调用的本地控制接口。

---

## 0. 先说结论：IM版本对不上

| 事实 | 依据 |
| --- | --- |
| ntchat 只支持IM **3.6.0.18** | ntchat 源码 `ntchat/wc/__init__.py` 里写死 `SUPPORT_VERSIONS = ['3.6.0.18']` |
| 本机装的是IM **4.2.0.7** | `C:\Program Files\Tencent\weixin\Weixin.exe` 的 FileVersion = 4.2.0.7 |
| IM 4.x 是重写的新客户端 | 进程名 `Weixin.exe`、数据目录 `xchat_files`，内部结构与 3.x 完全不同 |

ntchat 的原理是**把 DLL 注入 `IM.exe` 并 hook 内部函数**。IM 4.x 换了进程、换了模块、换了数据结构，3.6.0.18 的偏移量一个都对不上：

> **在IM 4.2.0.7 上，ntchat 不可能工作。** 这不是代码能绕过去的。

两条可行路线：

| 方案 | 代价 | 稳定性 |
| --- | --- | --- |
| **A. 加装IM 3.6.0.18**（推荐） | 装旧版IM + Python 3.10；**大概率还要绕过「版本过低」** | ★★★★★ 本仓库开箱即用 |
| **B. IM 4.x + omni-bot-sdk** | Python 3.12、DB 密钥、RPA 抢鼠标键盘 | ★★★ 见 `omni_plugin/` |
| C. IM 4.x + wxauto4 | 免费版最高 4.1.8.107，仍需降级 | ★★ 需要另写适配器 |

> **2026-10 实测补充**：IM服务端已经拒绝 3.6.0.18 登录，会弹「当前IM版本过低」。
> 方案 A 需要先用 `patch-version` 把内存版本号伪装成 3.9.12.15，见 2.0 节。
> 如果伪装也被拒，就只剩方案 B（omni，读数据库 + 视觉点按钮，不注入不 Hook）。

**要下载什么、多大、从哪下，全在 [DOWNLOADS.md](DOWNLOADS.md) 里**（含语音/看图模型的选型和省流量技巧）。

---

## 1. 组成

```
chat_bridge/
├─ dl_api.py                 # 数字生命 HTTP 服务：/chat /proactive /status
├─ run_bridge.py             # 入口：run / doctor / status / send / proactive / stickers
├─ web_ui.py                 # 网页版服务（不装IM也能聊）
├─ web/index.html            # 网页版前端（单文件，无 CDN）
├─ tools/make_demo_stickers.py  # 生成 6 张示例表情
├─ config.example.json
├─ requirements.txt          # ntchat
├─ requirements-media.txt    # faster-whisper + pillow（可选）
├─ DOWNLOADS.md              # ★ 下载清单
├─ bridge/
│  ├─ app.py                 # 装配层（含媒体线程池、指令分发）
│  ├─ config.py              # 配置解析与校验
│  ├─ client.py              # 调数字生命 API（urllib，带重试）
│  ├─ pipeline.py            # 清洗 / 唤醒词 / 去重 / 防自环
│  ├─ worker.py              # 串行回复队列 + 按情绪发表情
│  ├─ scheduler.py           # 主动聊天定时器
│  ├─ control.py             # 本地控制 API（DSH 用）
│  ├─ version_patch.py       # 绕过「IM版本过低」（改内存版本号）
│  ├─ media/
│  │  ├─ voice.py            # 语音转写（faster-whisper / 云端）
│  │  ├─ vision.py           # 图片描述（Ollama / 云端）
│  │  └─ router.py           # 媒体 → 一句话；顺手收藏表情
│  ├─ stickers/
│  │  ├─ store.py            # 表情仓库（去重/标签/挑选）
│  │  └─ commands.py         # /表情 指令
│  └─ adapters/
│     ├─ ntchat_adapter.py   # ★ 真实IM（文本+图片+语音+表情）
│     └─ mock.py             # 不碰IM，跑测试
├─ omni_plugin/              # 备选：IM 4.x + omni-bot-sdk 插件
│  ├─ omni_digital_life_plugin.py
│  ├─ pyproject.toml         # entry point: omni_bot.plugins
│  └─ config.example.yaml
└─ tests/                    # 72 个测试
```

数据流：

```
IM消息 ──► ntchat 回调 ──► pipeline（自己？重复？@我？唤醒词？）
                                 │
                    ┌────────────┴────────────┐
                 纯文本                    图片/语音/表情
                    │                          │
                    │                   媒体线程池（不堵队列）
                    │                   ASR / 视觉模型 → 一句话
                    │                          │
                    └────────────┬─────────────┘
                                 ▼
                        queue（单线程串行）
                                 ▼
                     POST /chat {"message": "...", "user_id": "wxid_..."}
                                 ▼
              reply ──► 私聊 send_text / 群聊 send_room_at(@对方)
                     └──► 按情绪挑一张表情 send_gif / send_image

定时器 ──► POST /proactive ──► 发给「文件传输助手」或指定群
```

---

## 2. 快速开始

### 2.0 先解决「当前IM版本过低」

装好 3.6.0.18 一扫号就弹这个，说明**服务端在拒绝旧版本**，不是配置问题。IM会把内存里的版本号（`0x63060012`）发给服务器，服务器一比就拒。绕过方法：把内存里的版本号改成 3.9.12.15（`0x63090C0F`），进程代码还是 3.6 的，ntchat 的 hook 偏移量不受影响。

```powershell
# 1) 打开IM 3.6.0.18，停在扫码界面（不要点登录）
# 2) 打补丁
pip install pymem
python run_bridge.py patch-version
# 3) 看到 ok:true 后去扫码登录
# 4) 登录成功再启动中间件
python run_bridge.py run --config config.json
```

也可以让中间件启动时自动打（在配置里）：

```json
"chat": { "version_spoof_enabled": true, "version_spoof_target": "3.9.12.15" }
```

⚠️ 这是绕过腾讯版本策略，**违反用户协议、有封号风险**，自己权衡；IM重启后要重新打一次。
如果 3.9.12.15 也被拒，说明服务端要求 4.x 协议，**改用 `omni_plugin/` 那条路**。

### 2.1 环境

| 项 | 要求 |
| --- | --- |
| Python | **3.10 / 3.11**，位数与IM一致（3.6.0.18 常见 32 位） |
| IM | **3.6.0.18** |
| 必需依赖 | `ntchat==0.1.20` |
| 可选依赖 | `faster-whisper`（语音）、`pillow`（缩图）、Ollama 视觉模型（看图） |

```powershell
cd PROJECT_ROOT\chat_bridge
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-media.txt      # 想要语音/看图再装
copy config.example.json config.json
python run_bridge.py doctor
```

`doctor` 会逐项检查：Python 位数、ntchat、IM版本、注册表、API、语音、视觉模型、表情包数量、端口，并给出结论。

### 2.2 启动（三个窗口）

```powershell
# 窗口 1：数字生命 API（127.0.0.1:8000）
python dl_api.py

# 窗口 2：IM中间件
python run_bridge.py run --config config.json

# 窗口 3：看状态
python run_bridge.py status --config config.json
```

### 2.3 验证

| 你做什么 | 预期 |
| --- | --- |
| 朋友私聊「在吗」 | 收到文字回复，有概率跟一张表情 |
| 朋友发语音 | 先转文字，再正常回复 |
| 朋友发图片 | 先描述图片，再回复 |
| 群里 `@机器人 在吗` 或 `她 在吗` | 群里回复并 @ 对方 |
| 群里随便聊天（没 @、没唤醒词） | 不理 |
| 自己发的消息 | 绝不回复（三重保险） |
| `/表情 发 开心`（管理员） | 发一张「开心」表情 |

---

## 3. 配置

`config.json` 每一项都有默认值，写错类型会直接报错并指出字段名。完整样例见 `config.example.json`。

### api

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:8000` | 数字生命服务 |
| `chat_path` / `proactive_path` | `/chat` `/proactive` | 接口路径 |
| `timeout` / `retries` | `120` / `1` | 超时与重试（大模型慢，别设太小） |
| `token` | `""` | 若 API 需要 Bearer 鉴权 |

`/chat` 还接受三个可选媒体字段：`image_path` / `audio_path` / `emoji_url`。给了就先做视觉描述或 ASR，拼成 `[图片] …` / `[语音] …` 再交给 digital_life。中间件和 omni 插件都走这条。

### chat

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `adapter` | `ntchat` | `ntchat` 或 `mock` |
| `chat_exe_path` | `""` | 自定义IM路径（多版本共存时用） |
| `wake_words` | `["她","小鱼"]` | 群聊唤醒词 |
| `owner_wxids` | `[]` | **能用 `/表情` 指令的人**（不填则指令关闭） |
| `allowed_rooms` / `allowed_wxids` / `blocked_wxids` | `[]` | 群/人白名单、黑名单 |
| `user_id_strategy` | `sender` | `sender`=每人一份记忆；`room`=每群一份；`fixed`=共用一个 |
| `min_reply_interval` / `send_gap` | `2.0` / `0.8` | 限速 |
| `max_reply_chars` | `1200` | 超长回复按句拆分 |
| `fallback_on_error` | `""` | API 挂了要不要回一句兜底 |
| `echo_guard_seconds` | `60` | 回显防御窗口 |

### media（语音/图片/表情总开关）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 关掉就只处理文字 |
| `voice_enabled` / `image_enabled` / `emoji_enabled` | `true` | 分项开关 |
| `workers` | `2` | 媒体处理线程数（不堵回复队列） |
| `placeholder_voice` / `placeholder_image` / `placeholder_emoji` | 见样例 | 处理失败时塞给对方的话 |

### asr（语音转文字）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `provider` | `faster-whisper` | `faster-whisper` / `openai` / `none` |
| `local_model` | `small` | `tiny/base/small/medium/large-v3` |
| `device` / `compute_type` | `cpu` / `int8` | 有 N 卡可改 `cuda` / `float16` |
| `download_root` | `""` | 模型缓存目录（离线部署用） |
| `language` | `zh` | 识别语言 |
| `base_url` / `api_key` / `api_model` | — | `provider=openai` 时用 |

### vision（看图）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `provider` | `ollama` | `ollama` / `openai` / `none` |
| `model` | `gemma3:4b` | 你机器上已有；中文更好用 `qwen2.5vl:3b` |
| `base_url` | `http://127.0.0.1:11434` | Ollama 地址 |
| `prompt` | 见样例 | 描述图片的提示词 |
| `max_side` | `1280` | 送模型前缩到这个边长（需要 Pillow） |
| `max_image_mb` | `8` | 超过直接放弃（回占位符） |

### stickers（表情包）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `dir` | `stickers` | 仓库目录（相对配置文件） |
| `auto_collect` / `collect_gif` / `collect_emoji` | `true` | 自动收藏收到的 GIF / IM表情 |
| `chance` | `0.25` | 每条回复后附一张表情的概率 |
| `mood_enabled` | `true` | 按情绪（joy/hurt/impulse）挑标签 |
| `max_kb` | `2048` | 单个表情体积上限 |
| `commands_enabled` | `true` | `/表情` 指令开关 |

### proactive / control / logging

见 `config.example.json`；`control.host` 只允许 `127.0.0.1 / localhost / ::1`（能发消息、能关进程，不暴露局域网）。

---

## 4. 关键行为

### 4.1 私聊

收到 → 清洗 → `POST /chat` → 取 `reply` → `send_text`。`user_id` 用对方 wxid，复用 digital_life 的多用户记忆隔离。

### 4.2 群聊

**触发**：被 `@`（`at_user_list` 含自己）或正文含唤醒词。
**清洗**：去 `wxid_xxx:` 前缀、去开头 `@昵称`（含 U+2005）、去唤醒词。
**回复**：`send_room_at_msg(to_wxid=群, content="{$@} 正文", at_list=[发送者wxid])`；@ 失败自动退化成普通群消息。

### 4.3 语音

ntchat 收到语音时 `data["mp3_file"]` 已是 mp3 → faster-whisper 直接转写 → 正文变成：

```
[语音] 今天好累啊，不想上班
```

转写失败只降级成 `[对方发来一条语音，你没听清]`，不会丢消息。**不需要 silk 解码器、不需要 ffmpeg。**

### 4.4 图片

`data["image"]` 是本地大图 → Pillow 缩到 1280px → Ollama 视觉模型 → 正文变成：

```
[图片] 一只橘猫趴在机械键盘上，屏幕还亮着
```

冷启动（模型没加载）第一张图可能要 30 秒，之后 0.5-2 秒。想快就常驻：`OLLAMA_KEEP_ALIVE=-1`。

### 4.5 表情包

**怎么攒**

1. 收到的 GIF 图、IM表情自动进 `stickers/`（sha1 去重、体积过滤、标签「未分类」）；
2. `/表情 存 开心`：把该会话上一条图片/表情存成「开心」；
3. `python run_bridge.py stickers import D:\表情包 --tags 开心,日常` 批量导入；
4. `python run_bridge.py stickers list` 看库存。

**怎么用**

- 回复时按情绪挑：`joy≥65 → 开心`、`hurt≥65 → 委屈`、`impulse≥75 → 生气`，否则「日常」；命中后按 `chance` 概率附一张；
- 模型在回复里写 `[表情:开心]` → 强制发「开心」，标签从正文里删掉；
- 用户说「发个表情」→ 必定发；
- 管理员可以 `/表情 发 开心`、`/表情 随机`。

**指令一览**（只有 `owner_wxids` 里的人能用）

```
/表情                 看标签和数量
/表情 发 开心          发一张
/表情 随机             随便发
/表情 存 开心,可爱     存上一条图片
/表情 删 <id>          删掉
```

### 4.6 防自环：三重保险

1. `from_wxid == 自己` → 丢；
2. 适配器再算一次 `is_self`；
3. 回显防御：60 秒内、同会话、内容相同 → 丢（`ignored_echo`）。

### 4.7 主动聊天

后台线程，`interval ± jitter`；安静时段跳过；`/proactive` 可立即触发；`min_gap_minutes` 防连发。

---

## 5. 和 DSH 集成

```powershell
Invoke-RestMethod http://127.0.0.1:8765/status

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/send -ContentType 'application/json' `
  -Body (@{ target = "文件传输助手"; text = "来自 DSH" } | ConvertTo-Json)

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/sticker -ContentType 'application/json' `
  -Body (@{ tag = "开心"; target = "文件传输助手" } | ConvertTo-Json)

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/proactive
```

| 接口 | 作用 |
| --- | --- |
| `GET /health` | 存活检查 |
| `GET /status` | 队列、计数、媒体/表情状态、最近消息 |
| `POST /send` | `{"target":"wxid/群名/文件传输助手","text":"..."}` |
| `POST /sticker` | `{"tag":"开心","target":"..."}` |
| `POST /proactive` | 触发一次主动聊天 |
| `POST /pause` | `{"paused":true/false}` |
| `POST /reload` | 热重载（API、过滤规则、主动聊天、表情包索引） |
| `POST /shutdown` | 优雅退出 |

---

## 6. 常见问题

**1) `IMVersionNotMatchError`**
装 3.6.0.18。还报就 `chat.chat_exe_path` 指定路径，或改注册表（`HKCU\SOFTWARE\Tencent\IM` 的 `Version` = `0x63060012`）。

**1.5) 扫码后弹「当前IM版本过低，请升级至最新版本」**
这是服务端拒绝旧版本，见 2.0 节：`python run_bridge.py patch-version`。
补丁只改内存，IM重启就没了；如果改成 3.9.12.15 仍被拒，只能换 omni 路线（第 8 节）。

**2) 注入失败 / `open()` 返回 False**
IM 4.x 还在跑 → 退出；3.6 没登录 → 先手动登录；Python 位数和IM不一致；安全软件拦截注入。

**3) 语音没反应，日志 `未安装 faster-whisper`**
`pip install faster-whisper`；确认 `asr.provider` 不是 `none`。首次识别会下载模型（约 500MB），耐心等一次。

**4) 图片描述很慢 / 超时**
Ollama 模型冷启动要 30 秒；先 `ollama run qwen2.5vl:3b "hi"` 预热，或设 `OLLAMA_KEEP_ALIVE=-1`，或换 `moondream:1.8b`。

**5) 表情一直存不进来**
- IM表情 CDN 有时需要登录态，下载失败会跳过（日志有记录）；
- 手动发一张 GIF，再 `/表情 存 开心` 最稳；
- 检查 `stickers.max_kb` 和格式（只收 gif/png/jpg/webp）。

**6) `/表情` 没反应**
`chat.owner_wxids` 没填，或发指令的人不在名单里（日志会写「非管理员尝试使用表情指令」）。

**7) 群里 @ 没反应**
`at_list` 必须是真实 wxid；日志里 `at=[...]` 能看到上报值。

**8) 中文乱码**
`chcp 65001` + `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`。文件日志本身就是 UTF-8。

**9) `sqlite3.ProgrammingError: SQLite objects created in a thread...`**
`dl_api.py` 已用单线程 executor 规避，正常不会再出现。

---

## 7. 测试

```powershell
python -m unittest discover -s tests -v
```

72 个用例：文本/群聊/@/唤醒词/自我过滤/回显/去重/白名单/长消息拆分、表情仓库（去重/标签/挑选/删除/持久化）、`/表情` 指令、媒体路由（语音/图片/失败降级/GIF 收藏）、版本伪装（版本号换算/失败路径）、端到端（假 API + mock 适配器：私聊、群 @、主动聊天、API 故障隔离、控制口全接口、表情指令）。

---

## 8. 备选路线：IM 4.x + omni-bot-sdk

如果 3.6.0.18 伪装版本也被拒，就只剩这条路：**不注入、不 Hook**，靠读IM数据库拿消息 + 视觉识别点按钮发消息。它天然支持 4.x（官方标 4.0.6.33，4.2.0.7 属「理论支持」）。

代价：

| 项 | 说明 |
| --- | --- |
| Python 3.12 | omni 硬要求，和 ntchat 的 3.10 是两个环境 |
| DB 密钥 | 要用 DbkeyHook 之类的工具拿 64 位密钥 |
| 抢鼠标键盘 | RPA 执行时人不能用电脑，建议单独一台机器 |
| 同名会话 | 按显示名切换，同名联系人可能发错 |
| 依赖体积 | 带 YOLO/OCR/torch，安装包体量不小 |
| 许可证 | GPL-V3，自己用没问题，二次分发注意 |

入口：[`omni_plugin/README.md`](omni_plugin/README.md)。插件已经写好（`pyproject.toml` 注册 `omni_bot.plugins` entry point），配置模板在 `omni_plugin/config.example.yaml`。

它复用同一套东西：同一个 `dl_api`、同一个 `stickers/` 目录、同一套唤醒词规则。

---

## 9. 网页版（不装IM也能试）

IM被版本卡住的时候，先用浏览器看效果。网页版不重复实现任何逻辑：消息转给 `dl_api`，表情读同一个 `stickers/` 目录。

```powershell
# 窗口 1：数字生命 API
#   --llm auto（默认）：优先用 DeepSeek（环境变量或 DSH 凭据），没有 key 再用本地 Ollama
python dl_api.py --vision-model gemma3:4b --llm auto

# 指定本地模型（不想花 API 额度）
python dl_api.py --llm ollama --llm-model qwen2.5:14b

# 窗口 2：网页
python web_ui.py                    # http://127.0.0.1:8899
```

> `--llm auto` 会读 `~/.dsh/.credentials.yaml` 里的 `DEEPSEEK_API_KEY`（DSH 自己用的那把），
> 用的是你的额度。不想这样：加 `--no-llm-from-dsh`，或 `--llm-api-key sk-...` 指定别的 key。

**时间**：`digital_life` 的 Prompt 里原本没有当前时间，模型只能瞎猜。现在每次对话前会把
`【现在】2026-10-28 00:11，周三，深夜。` 拼进人格，问"现在几点"她能答对，也不会半夜说早安。

打开 <http://127.0.0.1:8899>：

| 能做 | 说明 |
| --- | --- |
| 文字聊天 | 带打字中提示、消息历史（存 localStorage）、右上角可清空 |
| 分条回复 | 模型用 `\|\|\|` 断句，前端按真人节奏一条条发，不是一次性一大段 |
| 发图片 | 上传后走视觉模型，回复基于图片内容 |
| 语音输入 | 用浏览器自带识别（Chrome / Edge），说完按发送 |
| 表情包 | 默认 15% 概率、60 秒冷却；点「发个表情」强制要 |
| 情绪条 | 顶部实时显示 joy / hurt / impulse |
| 主动消息 | 「让她主动说一句」按钮，等价于 `/proactive` |

**关于"像不像真人"**：

- 回复开头的 `（我认真想了想）` 这类旁白会被自动剥掉（`--keep-stage-directions` 可保留）；
- 人格后面默认追加了一段"说话方式"提示（别写旁白、别给建议、别复述），可以用
  `--persona-extra "你的风格"` 覆盖，传空字符串则不加；
- **节奏**：服务端每次随机给一个停顿（默认 0.8-3.5 秒，8% 概率拖到 8-20 秒），网页按 `delay_ms` 等，抖音插件会一直等 HTTP 响应，所以消息也会晚到——看起来就像她去忙别的了；
- **每条 ≤20 字**：聊天和梦境都按 20 字一条拆开（`--max-bubble` 可调），一条条慢慢发；新闻标题放宽到 40，避免把词切碎；
- **已读不回**：5% 概率她看完不回（`--ignore-chance` 可调），网页上显示一行「…… 她看了，没回」；
- **梦境**：换成自定义生成器——60-90 字、只写一个场景，明确禁止「镜子/迷宫/深渊/怪物/碎片/撕裂/吞噬/扭曲」这些陈词滥调，梦见的是你们当天聊过的事，而不是通用恐怖片；
- **长期记忆**：每次对话会从记忆库里挑最重要的 `--memory-digest 5` 条书皮（每条约 24 字，总共 ≤160 字）拼进人格，跨会话也不断片；嫌费 token 可以设 `--memory-digest 0`，让她只靠检索。

- 本地 3B/4B 模型的上限就在那儿：语气能顺，但容易带点客套。想更像，二选一：
  - `ollama pull qwen2.5:7b` 后 `--llm-model qwen2.5:14b`（更稳，CPU 上慢一些）；
  - 设 `DEEPSEEK_API_KEY` 用云端（最好，但要联网）。

- 每个浏览器会话有自己的 `user_id`（`web-xxxxxxxx`），记忆独立于IM；想共用就把 localStorage 里的值改成对方的 wxid，或把 `dl_api` 的 `user_id_strategy` 换成 `fixed`。
- 第一次打开先跑一次 `python tools/make_demo_stickers.py`，会生成 6 张示例表情（开心/委屈/生气/日常/失落/无语）。换成自己的：`python run_bridge.py stickers import 目录 --tags 开心`。

---

## 10. 抖音：复用已装的插件

你机器上已经装了 `dsh-douyin-browser-local@1.4.5`（DSH 桌面 profile 里），它负责用 Playwright 打开抖音网页版、读私信、发消息。它本来用自己的模型回复私信；我打了个补丁，让**私信先走她**，失败再回退到它原来的模型。

```powershell
python tools/patch_douyin_plugin.py            # 打补丁（已备份）
python tools/patch_douyin_plugin.py --revert   # 还原
```

- 改的是 `~/.dsh/profiles/desktop/node_modules/dsh-douyin-browser-local/chat-agent.mjs`，只在 `replyProvider` 前加了一段 `fetch`，原逻辑完整保留；
- **看视频、点赞、评论仍然用它自己的 DSH 模型**，不受影响；
- 重启 DSH 后生效；插件升级会覆盖补丁，升级后重跑一次；
- 记忆按抖音会话的 `displayName` 隔离（改名会换一份记忆）；
- 它要求回复 ≤ 1000 字、不能声称"我是用户本人"，我们的回复会自动被它校验；不合规就不发。

### 10.1 把记忆库接进抖音助手

抖音助手有自己的共享记忆库：`~/.dsh/douyin-browser/memory/shared.sqlite`（表 `memory_entries`）。数字生命的记忆在 `data/xiaoyu.db` 的 `books` 表里。用同步脚本把两边接起来：

```powershell
python tools/sync_memory.py --min-mv 30 --limit 40      # 同步一次
python tools/sync_memory.py --watch 300                  # 每 5 分钟增量同步
```

它只做一件事：把重要度高的书皮（`summary`）写成 `kind=note` 的记忆条目，`external_key` 去重，重复跑不会增加行。写完以后，助手的 `douyin_recall` 就能检索到，例如：

> 查「加班 失眠」→ 命中「对方说：我最近失眠，老是三点才睡」（source=digital-life）

注意两点：

- 助手的检索只认 `origin='video'`，所以同步条目也写成 `video`（这是它自己存笔记时的惯例），但 `kind=note`，不会被当成看过的视频；
- 人格卡 `memory/persona/role-card.md` 里目前是「小鲸」，和数字生命的人格是分开的。私信回复走我们的接口时用的是数字生命的人格，不受这张卡影响；要让助手本体的任务对话也叫「小鱼」，得改那张卡——需要的话说一声。

**转发视频为什么像机器人**：这个插件的自主分享是**只发官方卡片、不带文字**的（它的注释写得很明白：选择理由留在本机，不进入私信正文）。所以看起来就是冷不丁甩个视频过来。

### 10.2 人格卡换成小鱼

```powershell
python tools/patch_douyin_persona.py            # 换人格（先备份）
python tools/patch_douyin_persona.py --revert   # 还原
python tools/patch_douyin_persona.py --dry-run  # 先看会改成什么
```

只改 `<!-- DOUYIN_PERSONA_GENERATED:START -->` 之前的「手动区」，自动整理的代理兴趣/观看记录原样保留。改完重启 DSH 生效。

### 10.3 反向同步：她刷到的也变成她的记忆

```powershell
python tools/sync_from_douyin.py                # 同步一次
python tools/sync_from_douyin.py --watch 300    # 每 5 分钟增量
python tools/sync_from_douyin.py --user douyin  # 写到指定 user_id 名下
```

把助手里 `source != 'digital-life'` 的条目（看过的作品、记下的笔记）写进 `books`：

- 书皮形如「刷到 @小咪酱：8月22日捡到一只小三花」；
- mv 按互动给分：喜欢 30、路过 20、划走 16，看完再加 6，最高 45；
- 交给 digital_life 自己的三阶遗忘去衰减（mv<30 变摘要、<10 变幽灵、14 天消失），不会无限膨胀；
- 用 `data/.douyin_reverse_sync.json` 记已同步的 `id:updated_at`，重复跑不重复写。

实测：同步后以 `user_id=douyin-assistant` 问她「你最近刷到什么好玩的了」，她会答「刷到个捡猫的，还挺有意思。那猫后来咋样了我没看完。」

我准备了一个文案接口，让它能"说一句"：

```powershell
# 结合她的记忆，给这个视频配一句话
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/share -ContentType 'application/json' `
  -Body (@{ title="凌晨三点的便利店"; author="阿树"; user_id="douyin-某某" } | ConvertTo-Json)
# -> {"ok":true,"text":"刷到这个，想起你三点还没睡","delay_ms":2178}
```

实测效果：

| 视频 | 她配的话 |
| --- | --- |
| 凌晨三点的便利店 | 刷到这个，想起你三点还没睡 |
| 成年人的崩溃瞬间 | 这个视频好像你，失眠还硬撑到三点。 |

把这句话接到插件的分享流程里，需要再改一处 `autonomous-share`（在卡片前后补一条 `chat-service.sendMessage`）——这动的是它"发送回执/去重"的链路，我想单独做一次并让你在真实会话里验，避免把回执搞乱。要做的话说一声。

也可以把 `dl_api` 当通用 OpenAI 接口给别的客户端用：

```
base_url: http://127.0.0.1:8000/v1
model:    xiaoyu
api_key:  随便填
```

### 10.4 逐字输入与「对方正在输入」

抖音私信之前的问题是两个：

1. `CHAT_SEND_BUTTON_UNAVAILABLE`：发送按钮选择器命中了隐藏副本，`count() !== 1`，于是答了也不发；
2. `page.screenshot: Timeout 5000ms`：后台截图超时，联系人被自动暂停。

补丁：`python tools/patch_douyin_typing.py`（`--revert` 还原）

- 把 `editor.fill(message)` 换成**一个字一个字 `insertText`**，间隔 55-130ms（长文本自动降到 12ms），抖音会显示「对方正在输入…」，也降低粘贴式发送被风控的概率；
- 发送按钮选择器加 `:visible`，不再被隐藏副本干扰；
- 截图超时 5s → 15s，减少后台卡顿导致的暂停。

改完**重启 DSH**，再启动私信自动回复才会用新代码。

## 11. 边界与合规

- 只用于**你自己有权控制的账号和设备**，别拿去群发、营销、骚扰；
- 先用「文件传输助手」和一个测试群验证，再放开真实联系人；
- 语音/图片走本地模型时，数据不出本机；换成云端 API 就是另一回事，自己权衡；
- 控制口只绑 127.0.0.1；多人共用这台机器时务必设 `control.token`；
- IM对自动化并不友好，**账号风险自担**。
