# MaiBot MiniMax 音乐生成插件

基于 MiniMax Music API 的音乐生成插件，为 MaiBot 提供人声歌曲、纯音乐、翻唱生成能力，并内置 buddy-sings 功能让 bot 用独特嗓音唱歌。

## 功能特性

- 🎵 **人声生成**：支持自定义歌词或自动生成，生成带歌词的人声歌曲
- 🎸 **纯音乐生成**：无歌词背景音乐，适合场景配乐
- 🎤 **翻唱生成**：基于用户提供的音频直链 URL，调用 MiniMax 翻唱模型生成新版本（`cover_song` 工具）
- 🐾 **buddy-sings**：基于 bot 人格构建专属声音身份，让 bot 用独特嗓音唱歌
- 🔄 **配置热重载**：修改配置无需重启，自动生效（插件配置 + 全局 Bot 配置）
- 📁 **智能输出**：音频自动保存到数据目录，文件名含时间戳
- 🔁 **生产级可靠性**：aiohttp session 复用、错误码分类、指数退避重试
- 🌐 **完整本地化**：用户界面简体中文，API prompt 英文（最佳生成质量）

## 安装

### 1. 克隆插件到 MaiBot 插件目录

```bash
cd <MaiBot根目录>/plugins
git clone https://github.com/maibot-community/maibot-minimax-music.git
```

### 2. 安装依赖

```bash
cd maibot-minimax-music
pip install -r requirements.txt
```

测试依赖（可选）：

```bash
pip install pytest pytest-asyncio aioresponses
```

### 3. 配置 API Key

首次启动 MaiBot 后，插件会自动生成 `config.toml`。编辑它填入你的 MiniMax API Key：

```bash
vi config.toml
```

将 `[music]` 下的 `minimax_api_key` 填入你的 API Key（在 https://platform.minimaxi.com 申请）。

### 4. 重启 MaiBot

```bash
# 重启 MaiBot，插件将自动加载
```

## 配置说明

插件配置分为三个分组：`[plugin]`（插件基础）、`[music]`（音乐生成）、`[buddy]`（宠物唱歌）。首次启动时若 `config.toml` 不存在，会自动从 `config.example.toml` 复制生成。

### `[plugin]` 插件基础设置

| 参数名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | `true` | 是否启用插件 |
| `config_version` | string | `"1.0.0"` | 配置版本号 |

### `[music]` 音乐生成设置

| 参数名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `minimax_api_key` | string | `""` | MiniMax API Key（必填，留空将禁用插件，在 https://platform.minimaxi.com 申请） |
| `api_base_url` | string | `"https://api.minimaxi.com"` | MiniMax API 地址（国内版，国际版用 `https://api.minimax.io`） |
| `model` | string | `"music-2.6-free"` | 音乐模型：`music-2.6-free` / `music-2.5+` / `music-2.5`（instrumental 仅 2.5+/2.6-free 支持） |
| `output_dir` | string | `""` | 音频输出目录，留空则使用插件数据目录下的 `output/` |
| `audio_format` | string | `"mp3"` | 音频格式：`mp3` / `wav` / `pcm` |
| `sample_rate` | int | `44100` | 采样率 |
| `bitrate` | int | `256000` | 比特率（仅 mp3 生效） |
| `send_mode` | string | `"record"` | 发送方式：`record`(语音消息) / `file`(文件消息) / `text`(仅文本提示)；record 适合 QQ NapCat，失败会自动回退 |
| `max_retries` | int | `3` | API 调用最大重试次数（仅对限流/网络错误重试） |
| `retry_backoff_base` | float | `1.5` | 重试退避基数（指数退避：`base ** attempt + 随机抖动`） |

### `[buddy]` 宠物唱歌设置

| 参数名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | `true` | 是否启用 buddy_sings 工具 |
| `voice_cache_dir` | string | `""` | 声音身份缓存目录，留空则使用插件数据目录下的 `voices/` |
| `default_language` | string | `"zh"` | 歌词默认语言（`zh`/`en`/`ja`/`ko` 等，影响 prompt_fragment 中的语言声明） |
| `fallback_nickname` | string | `"麦麦"` | 读取全局 Bot 配置失败时的回退昵称 |
| `fallback_personality` | string | `""` | 读取全局 Bot 配置失败时的回退人格描述 |

## 工具说明

插件暴露两个 `@Tool` 供 LLM 自主调用，LLM 会根据对话上下文判断何时使用。`stream_id` 由插件自动从消息上下文解析，无需 LLM 显式传入。

### `generate_song` 工具

生成一首歌曲（人声或纯音乐），覆盖 minimax-music-gen 技能的 Basic 与 Advanced 模式。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `prompt` | string | ✅ | - | 音乐风格描述，建议用英文（如 `indie folk, melancholic, acoustic guitar`） |
| `mode` | string | ❌ | `vocal` | 生成模式：`vocal`（人声歌曲）或 `instrumental`（纯音乐） |
| `lyrics` | string | ❌ | `""` | 自定义歌词，提供时直接使用（不启用自动生成） |
| `auto_lyrics` | string | ❌ | `true` | `mode=vocal` 且无 `lyrics` 时是否自动生成歌词（`true`/`false`） |
| `genre` | string | ❌ | `""` | （可选）音乐流派，如 `folk`、`EDM`、`jazz` |
| `mood` | string | ❌ | `""` | （可选）情绪，如 `melancholic`、`uplifting`、`calm` |
| `vocals` | string | ❌ | `""` | （可选）人声描述，如 `warm female voice`、`deep male voice` |
| `instruments` | string | ❌ | `""` | （可选）乐器，如 `acoustic guitar and piano` |
| `bpm` | string | ❌ | `""` | （可选）节拍速度，如 `90 BPM` |
| `msg_id` | string | ❌ | `""` | 当前消息 ID |

**调用示例**（LLM 自主决定）：

- 让 LLM 生成一首民谣：LLM 调用 `generate_song(prompt="indie folk, melancholic, fingerpicked acoustic guitar, soft female vocal", mode="vocal", genre="folk", mood="melancholic", instruments="acoustic guitar", bpm="72 BPM")`，插件自动生成歌词、合成人声并保存为 `YYYYMMDD_HHMMSS_<slug>.mp3`，通过语音消息发送。
- 生成一段纯音乐配乐：LLM 调用 `generate_song(prompt="jazz piano, relaxing, late night cafe", mode="instrumental")`，生成无歌词背景音乐。
- 使用自定义歌词：LLM 调用 `generate_song(prompt="upbeat synth-pop", mode="vocal", lyrics="[verse]\nWalking through the neon light...")`，使用传入歌词合成。

> `mode=instrumental` 仅 `music-2.5+` / `music-2.6-free` 模型支持，`music-2.5` 不支持时会返回明确错误。

### `cover_song` 工具

基于用户提供的参考音频 URL 进行翻唱生成。用户发一个音频直链，插件调用 MiniMax 翻唱模型生成新版本。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `prompt` | string | 是 | - | 翻唱风格描述，建议英文（如 `acoustic cover with soft piano, slower tempo`） |
| `audio_url` | string | 是 | - | 参考音频的直链 URL，需为可直接下载的音频文件（`.mp3`/`.wav` 等） |
| `lyrics` | string | 否 | `""` | 自定义歌词，不传则保持原曲歌词 |
| `msg_id` | string | 否 | `""` | 当前消息 ID |

**使用示例**：
- 用户发链接翻唱：用户发送「帮我翻唱这首歌 https://example.com/song.mp3，改成钢琴版」，LLM 调用 `cover_song(prompt="soft piano cover, slower tempo, gentle", audio_url="https://example.com/song.mp3")`，插件生成翻唱版本并保存为 `YYYYMMDD_HHMMSS_<slug>.mp3`，通过语音消息发送。
- 自定义歌词翻唱：LLM 调用 `cover_song(prompt="acoustic cover", audio_url="https://example.com/song.mp3", lyrics="[verse]\n自定义歌词...")`。

> **注意**：`audio_url` 必须是可直接下载的音频文件直链，不能是网页链接（如网易云/QQ音乐的歌曲页面 URL），需用直链下载地址。

### `buddy_sings` 工具

让 bot 用独特嗓音唱一首歌给用户，实现 buddy-sings 技能体验。基于 bot 人格（`[bot].nickname` + `[personality].personality`）构建专属声音身份并缓存，根据主题匹配音乐风格，生成 bot 第一人称视角的歌曲。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `theme` | string | ❌ | `""` | 歌曲主题，如 `今天的工作`、`深夜思念`、`鼓励`、`庆祝`；不传则根据人格特征随机选主题 |
| `custom_lyrics` | string | ❌ | `""` | 自定义歌词，应为 bot 第一人称视角（I=bot，you=user）；提供时不自动生成 |
| `regenerate` | string | ❌ | `false` | 是否重新构建声音身份（`true`/`false`），用于修改 bot 人格后强制刷新声音 |
| `msg_id` | string | ❌ | `""` | 当前消息 ID |

**主题 → 风格映射**：

| 主题语义 | 选用风格 | 避免风格 |
|----------|----------|----------|
| 鼓励 / 激励 / 加油 | synth-pop / funk / indie rock | - |
| 思念 / 等待 / 想念 | folk / R&B / lo-fi | rock / EDM |
| 深夜 / 安静 / midnight | ambient / lo-fi / neoclassical | upbeat / EDM |
| 庆祝 / 成就 / party | EDM / future bass / K-pop | 慢板 / 忧郁 |
| 吐槽 / 抱怨 / rant | funk / rap | - |
| 日常 / 无法识别 | city pop / bossa nova | - |

> 反单调规则：连续两次不会选用同一 genre。`theme` 为空时根据人格特征选择（安静→午夜安眠曲，活泼→冒险歌，神秘→月光小夜曲）并告知用户。

**调用示例**（LLM 自主决定）：

- 让 bot 唱一首关于深夜的歌：LLM 调用 `buddy_sings(theme="深夜思念")`，插件读取缓存的 bot 声音身份、匹配 ambient/lo-fi 风格、自动生成 bot 第一人称歌词、合成歌曲，保存为 `<bot_nickname>_sings_YYYYMMDD_HHMMSS.mp3` 并发送。
- bot 人格修改后重新唱歌：LLM 调用 `buddy_sings(theme="鼓励", regenerate="true")`，删除旧声音身份缓存并基于新人格重建。
- 使用自定义歌词：LLM 调用 `buddy_sings(theme="今天的工作", custom_lyrics="[verse]\nI sit by the door, waiting for you...")`，使用传入歌词（bot 第一人称）。

### `minimax_music_generate` API（供其他插件调用）

通过 `@API` 暴露给其他插件程序化调用音乐生成能力，不自动发送消息、不保存文件（由调用方决定）。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `prompt` | string | ✅ | - | 音乐风格描述（建议英文） |
| `lyrics` | string | ❌ | `""` | 自定义歌词，提供时不启用自动生成 |
| `is_instrumental` | bool | ❌ | `False` | 是否生成纯音乐 |
| `lyrics_optimizer` | bool | ❌ | `False` | 是否由 MiniMax 自动生成歌词 |
| `sample_rate` | int | ❌ | `44100` | 采样率 |
| `bitrate` | int | ❌ | `256000` | 比特率 |
| `fmt` | string | ❌ | `"mp3"` | 音频格式 |

**返回值**：

```python
# 成功
{"success": True, "audio_base64": "<str>", "format": "mp3", "duration": <ms>, "size": <bytes>}
# 失败
{"success": False, "error": "<msg>", "code": <status_code>}
```

**调用示例**（其他插件内）：

```python
result = await ctx.api.call(
    "com.maibot.minimax-music.minimax_music_generate",
    prompt="indie folk, warm acoustic guitar",
    lyrics="[verse]\n...",
    is_instrumental=False,
    lyrics_optimizer=False,
)
if result.get("success"):
    audio_base64 = result["audio_base64"]
    # 由调用方决定如何发送/保存
```

## 错误码

| 状态码 | 含义 | 处理建议 | 是否重试 |
|--------|------|----------|----------|
| `0` | 成功 | - | - |
| `1002` | QPS 限流 | 稍后重试 | ✅ 是 |
| `1004` | 鉴权失败 | 检查 API Key | ❌ 否 |
| `1008` | 余额不足 | 充值 | ❌ 否 |
| `1026` | 内容违规 | 修改 prompt / 歌词 | ❌ 否 |
| `2013` | 参数错误 | 检查参数（如 instrumental 用了 music-2.5） | ❌ 否 |

> 仅 `1002`（限流）与网络异常会触发指数退避重试（`retry_backoff_base ** attempt + 随机抖动`，最多 `max_retries` 次）；致命错误（`1004`/`1008`/`1026`/`2013`）直接失败并记录日志，不再重试。未知错误码归为致命错误。

## 平台适配器要求

音频发送方式由 `music.send_mode` 配置决定，不同方式对平台适配器的要求：

| 发送方式 | 消息类型 | NapCat | Lagrange | 其他适配器 | 适用场景 |
|----------|----------|--------|----------|------------|----------|
| `record`（默认） | 语音消息 | ✅ 支持 | ✅ 支持 | 视适配器 | QQ 群/私聊，接收方可直接内联播放 |
| `file` | 文件消息 | ✅ 支持 | ⚠️ 部分支持 | 视适配器 | 长歌曲或非 QQ 平台，接收方下载后播放 |
| `text` | 文本提示 | ✅ 所有平台 | ✅ 所有平台 | ✅ 所有平台 | 最可靠兜底，仅发送文件名通知 |

**说明**：

- `record` 模式通过 OneBot 11 的 `record` 消息段发送，NapCat/Lagrange 均支持
- `file` 模式通过 OneBot 11 的 `file` 消息段发送，依赖适配器支持
- 所有模式失败时会自动链式回退：`record → voice → file → text`，确保用户至少收到通知
- 若发送失败，音频文件仍会保存到输出目录，可通过文件路径手动获取

## 注意事项

1. **API Key 申请**：需在 [MiniMax 开放平台](https://platform.minimaxi.com) 申请，个人/企业认证后可用；国际版用户改用 `https://api.minimax.io` 并相应修改 `api_base_url`
2. **模型支持**：默认 `music-2.6-free`；纯音乐（`instrumental`）仅 `music-2.5+` / `music-2.6-free` 支持，`music-2.5` 不支持
3. **生成耗时**：音乐生成通常需要 30–120 秒，请耐心等待；超时由 `max_retries` 与指数退避覆盖网络瞬时错误
4. **文件管理**：输出目录会累积 mp3 文件，建议定期清理（路径见日志或返回的 `file_path`）
5. **buddy-sings 声音身份缓存**：修改 bot 人格后会自动检测失效并重建；也可用 `regenerate="true"` 手动重建
6. **配置热重载**：修改 `config.toml` 后自动生效，无需重启 MaiBot；修改全局 Bot 配置（昵称/人格）也会同步更新声音身份
7. **文本长度限制**：`prompt` 上限 2000 字符；`lyrics` 上限 3500 字符且不可为空，超出会返回明确错误
8. **费用**：按 MiniMax 音乐生成定价计费，详见 MiniMax 官方定价

## 目录结构

```
maibot-minimax-music/
├── _manifest.json          # 插件清单
├── plugin.py               # 主插件文件（工具/API/生命周期）
├── music_service.py        # MiniMax 音乐生成服务
├── voice_identity.py       # 声音身份管理
├── config.example.toml     # 配置模板
├── requirements.txt        # 依赖
├── README.md               # 本文档
├── LICENSE                 # MIT 许可证
├── tests/                  # 测试套件
│   ├── conftest.py
│   ├── test_music_service.py
│   ├── test_voice_identity.py
│   └── test_plugin.py
└── (运行时生成，gitignored)
    ├── config.toml          # 用户配置（从 config.example.toml 复制）
    ├── voices/              # 声音身份缓存
    └── output/              # 音频输出
```

## 更新日志

### v1.0.0

- 初始版本
- 支持人声生成、纯音乐生成、翻唱生成
- 支持 buddy-sings（bot 唱歌）功能
- 配置热重载（插件配置 + 全局 Bot 配置）
- 生产级可靠性（重试、错误码分类、资源清理）
- 完整测试套件

## 许可证

MIT License
