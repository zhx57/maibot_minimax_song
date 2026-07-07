"""MaiBot MiniMax 音乐生成插件。

基于 MiniMax Music API 实现音乐生成（人声/纯音乐）、翻唱生成、
buddy-sings（宠物唱歌）能力，借鉴 maibot_voice 的生产级插件结构：
aiohttp session 复用、错误码分类、指数退避重试、配置热重载、自动配置初始化。

暴露能力：
- @Tool generate_song: LLM 自主调用的人声/纯音乐生成
- @Tool buddy_sings: 让 bot 用独特嗓音唱歌
- @API minimax_music_generate: 供其他插件程序化调用
"""

import asyncio
import base64
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Iterable, Optional

from maibot_sdk import (
    API,
    CONFIG_RELOAD_SCOPE_SELF,
    Field,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType

try:
    from maibot_sdk import ON_BOT_CONFIG_RELOAD
except ImportError:
    ON_BOT_CONFIG_RELOAD = "bot"

try:
    from .music_service import MiniMaxMusicService
    from .voice_identity import VoiceIdentityManager
except ImportError:
    from music_service import MiniMaxMusicService
    from voice_identity import VoiceIdentityManager


# ====================================================================
# 配置模型
# ====================================================================


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本号")


class MusicSectionConfig(PluginConfigBase):
    __ui_label__ = "音乐生成"
    __ui_icon__ = "music_note"

    minimax_api_key: str = Field(default="", description="MiniMax API Key")
    api_base_url: str = Field(default="https://api.minimaxi.com", description="MiniMax API 地址")
    model: str = Field(default="music-2.6-free", description="音乐模型")
    output_dir: str = Field(default="", description="音频输出目录，空则用 data_dir/output")
    audio_format: str = Field(default="mp3", description="音频格式")
    sample_rate: int = Field(default=44100, description="采样率")
    bitrate: int = Field(default=256000, description="比特率")
    send_mode: str = Field(default="record", description="发送方式：record/file/text")
    max_retries: int = Field(default=3, description="最大重试次数")
    retry_backoff_base: float = Field(default=1.5, description="重试退避基数")


class BuddySectionConfig(PluginConfigBase):
    __ui_label__ = "宠物唱歌"
    __ui_icon__ = "pets"

    enabled: bool = Field(default=True, description="是否启用 buddy_sings")
    voice_cache_dir: str = Field(default="", description="声音身份缓存目录，空则用 data_dir/voices")
    default_language: str = Field(default="zh", description="歌词默认语言")
    fallback_nickname: str = Field(default="麦麦", description="回退昵称")
    fallback_personality: str = Field(default="", description="回退人格描述")


class MusicPluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    music: MusicSectionConfig = Field(default_factory=MusicSectionConfig)
    buddy: BuddySectionConfig = Field(default_factory=BuddySectionConfig)


# ====================================================================
# 辅助函数
# ====================================================================


def _to_bool(val: Any) -> bool:
    """将各种类型的值转为 bool（兼容 SDK 传 string 的情况）。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).strip().lower() in ("true", "1", "yes", "y", "t")


def _format_duration(duration: Any) -> str:
    """将毫秒时长格式化为秒字符串。"""
    try:
        sec = float(duration) / 1000
        return f"{sec:.1f} 秒"
    except (TypeError, ValueError):
        return "未知"


# ====================================================================
# 插件类
# ====================================================================


class MiniMaxMusicPlugin(MaiBotPlugin):
    """MaiBot MiniMax 音乐生成插件。"""

    config_model = MusicPluginConfig
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    def __init__(self) -> None:
        super().__init__()
        self.music_service: Optional[MiniMaxMusicService] = None
        self.voice_mgr: Optional[VoiceIdentityManager] = None
        self._bot_nickname: str = ""
        self._bot_personality: str = ""
        self._last_genre: str = ""
        self._enabled: bool = True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self._ensure_config_exists()

        music_cfg = self.config.music
        buddy_cfg = self.config.buddy

        # 校验 api_key
        if not music_cfg.minimax_api_key:
            self.ctx.logger.warning("MiniMax API Key 未配置，插件已禁用")
            self._enabled = False
            return

        self._enabled = True

        # 初始化 music_service
        self.music_service = MiniMaxMusicService(
            api_key=music_cfg.minimax_api_key,
            api_base_url=music_cfg.api_base_url,
            model=music_cfg.model,
            max_retries=music_cfg.max_retries,
            retry_backoff_base=music_cfg.retry_backoff_base,
            logger=self.ctx.logger,
        )

        # 解析 voice_cache_dir 并初始化 voice_mgr
        voice_cache_dir = self._resolve_voice_cache_dir()
        self.voice_mgr = VoiceIdentityManager(
            cache_dir=voice_cache_dir,
            logger=self.ctx.logger,
        )

        # 初始化 _bot_nickname / _bot_personality 为 fallback 值
        self._bot_nickname = buddy_cfg.fallback_nickname
        self._bot_personality = buddy_cfg.fallback_personality

        # 防御性尝试：从全局 Bot 配置读取初始值（文档矛盾，best-effort）
        try:
            all_cfg = self.ctx.config.get_all()
            if asyncio.iscoroutine(all_cfg):
                all_cfg = await all_cfg
            if isinstance(all_cfg, dict):
                nick = self._extract_bot_field(all_cfg, "nickname", ["bot_name", "name"])
                pers = self._extract_bot_field(all_cfg, "personality", ["bot_persona", "persona"])
                if nick:
                    self._bot_nickname = nick
                if pers:
                    self._bot_personality = pers
        except Exception as e:
            self.ctx.logger.warning("读取全局 Bot 配置失败，使用 fallback 值：%s", e)

        # 创建输出目录
        output_dir = self._resolve_output_dir()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.ctx.logger.warning("创建输出目录失败：%s", e)

        self.ctx.logger.info("MaiBot MiniMax 音乐生成插件已加载")

    async def on_unload(self) -> None:
        if self.music_service:
            await self.music_service.close()
            self.music_service = None
        if self.voice_mgr:
            self.voice_mgr.close()
            self.voice_mgr = None
        self._enabled = False
        self.ctx.logger.info("MaiBot MiniMax 音乐生成插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self._on_self_config_update()
        elif scope == ON_BOT_CONFIG_RELOAD or scope == "bot":
            await self._on_bot_config_update(config_data)

    async def _on_self_config_update(self) -> None:
        """插件自身配置热重载。"""
        music_cfg = self.config.music

        if not music_cfg.minimax_api_key:
            self.ctx.logger.warning("MiniMax API Key 未配置，插件已禁用")
            self._enabled = False
            if self.music_service:
                await self.music_service.close()
                self.music_service = None
        else:
            self._enabled = True
            if self.music_service:
                self.music_service.update_api_key(music_cfg.minimax_api_key)
                self.music_service.update_api_base_url(music_cfg.api_base_url)
                self.music_service.update_model(music_cfg.model)
            else:
                self.music_service = MiniMaxMusicService(
                    api_key=music_cfg.minimax_api_key,
                    api_base_url=music_cfg.api_base_url,
                    model=music_cfg.model,
                    max_retries=music_cfg.max_retries,
                    retry_backoff_base=music_cfg.retry_backoff_base,
                    logger=self.ctx.logger,
                )

        # voice_mgr 没有 update 方法，重建（磁盘缓存保留）
        voice_cache_dir = self._resolve_voice_cache_dir()
        if self.voice_mgr:
            self.voice_mgr.close()
        self.voice_mgr = VoiceIdentityManager(
            cache_dir=voice_cache_dir,
            logger=self.ctx.logger,
        )

        # 确保 _bot_nickname / _bot_personality 有值（on_load 可能因缺 API Key 提前返回）
        if not self._bot_nickname:
            self._bot_nickname = self.config.buddy.fallback_nickname
        if not self._bot_personality:
            self._bot_personality = self.config.buddy.fallback_personality

        self.ctx.logger.info("插件配置已热重载")

    async def _on_bot_config_update(self, config_data: dict[str, object]) -> None:
        """全局 Bot 配置热重载：更新 bot 人格。"""
        nickname = self._extract_bot_field(config_data, "nickname", ["bot_name", "name"])
        personality = self._extract_bot_field(config_data, "personality", ["bot_persona", "persona"])

        if nickname:
            self._bot_nickname = nickname
        if personality:
            self._bot_personality = personality

        self.ctx.logger.info("Bot 人格已更新：%s", self._bot_nickname)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _ensure_config_exists(self) -> None:
        """若 config.toml 不存在则从 config.example.toml 复制（不覆盖已有）。"""
        plugin_dir = Path(__file__).parent
        config_path = plugin_dir / "config.toml"
        example_path = plugin_dir / "config.example.toml"
        if config_path.exists():
            return
        if example_path.exists():
            shutil.copy2(example_path, config_path)
            self.ctx.logger.info("已从 config.example.toml 生成 config.toml")
        else:
            self.ctx.logger.warning("config.toml 与 config.example.toml 均不存在")

    def _extract_bot_field(
        self,
        config_data: Any,
        primary_key: str,
        fallback_keys: list[str],
    ) -> str:
        """防御性解析全局 Bot 配置字段，处理文档矛盾。

        查找顺序：
        1. 任意嵌套 dict 中的 primary_key（如 bot.nickname / personality.personality）
        2. 扁平 config_data[primary_key]
        3. fallback_keys（嵌套 + 扁平）
        """
        if not isinstance(config_data, dict):
            return ""

        def _find_in(key: str) -> str:
            # 嵌套 dict
            for section_val in config_data.values():
                if isinstance(section_val, dict):
                    val = section_val.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
            # 扁平
            val = config_data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            return ""

        result = _find_in(primary_key)
        if result:
            return result
        for fb in fallback_keys:
            result = _find_in(fb)
            if result:
                return result
        return ""

    def _resolve_output_dir(self) -> Path:
        """解析音频输出目录。空则用 data_dir/output；非空按字面路径（相对路径基于插件目录）。"""
        output_dir = self.config.music.output_dir
        if output_dir:
            path = Path(output_dir)
            if not path.is_absolute():
                path = Path(__file__).parent / path
            return path
        return self.ctx.paths.data_dir / "output"

    def _resolve_voice_cache_dir(self) -> Path:
        """解析声音身份缓存目录。空则用 data_dir/voices；非空按字面路径。"""
        voice_cache_dir = self.config.buddy.voice_cache_dir
        if voice_cache_dir:
            return Path(voice_cache_dir)
        return self.ctx.paths.data_dir / "voices"

    async def _find_stream_id(self, **kwargs: Any) -> Optional[str]:
        """三段回退解析 stream_id：kwargs → message → group streams → private streams。"""
        # 1. kwargs.stream_id
        sid = kwargs.get("stream_id")
        if sid:
            return str(sid)

        # 2. kwargs.message.stream_id
        msg = kwargs.get("message")
        if msg is not None:
            if isinstance(msg, dict):
                sid = msg.get("stream_id")
            else:
                sid = getattr(msg, "stream_id", None)
            if sid:
                return str(sid)

        # 3. group streams
        try:
            streams = await self.ctx.chat.get_group_streams()
            if streams:
                first = streams[0]
                if isinstance(first, dict):
                    sid = first.get("stream_id")
                else:
                    sid = getattr(first, "stream_id", None)
                if sid:
                    return str(sid)
        except Exception as e:
            self.ctx.logger.warning("chat.get_group_streams 失败：%s", e)

        # 4. private streams
        try:
            streams = await self.ctx.chat.get_private_streams()
            if streams:
                first = streams[0]
                if isinstance(first, dict):
                    sid = first.get("stream_id")
                else:
                    sid = getattr(first, "stream_id", None)
                if sid:
                    return str(sid)
        except Exception as e:
            self.ctx.logger.warning("chat.get_private_streams 失败：%s", e)

        return None

    def _save_audio(self, audio_bytes: bytes, prompt: str, is_buddy: bool = False) -> Optional[Path]:
        """将音频字节保存到输出目录。

        - 普通模式：YYYYMMDD_HHMMSS_<slug>.mp3
        - buddy 模式：<bot_nickname>_sings_<YYYYMMDD_HHMMSS>.mp3

        异常时 log error 不抛出，返回 None。
        """
        try:
            output_dir = self._resolve_output_dir()
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            if is_buddy:
                nickname = self._bot_nickname or "bot"
                filename = f"{nickname}_sings_{timestamp}.mp3"
            else:
                slug = re.sub(r"[^\w\u4e00-\u9fff-]", "_", (prompt or "")[:20]) or "audio"
                filename = f"{timestamp}_{slug}.mp3"

            file_path = output_dir / filename
            with open(file_path, "wb") as f:
                f.write(audio_bytes)
            self.ctx.logger.info("音频已保存：%s（%d bytes）", file_path, len(audio_bytes))
            return file_path
        except Exception as e:
            self.ctx.logger.error("保存音频失败：%s", e)
            return None

    async def _send_audio(self, audio_base64: str, filename: str, stream_id: str) -> bool:
        """通过 ctx.send.custom 发送音频，按 send_mode 链式回退。

        回退链：record → voice → file → text（按配置 mode 调整首发顺序）。
        """
        if not stream_id:
            return False

        audio_data = f"base64://{audio_base64}"
        mode = self.config.music.send_mode

        attempts: list[tuple[str, dict[str, Any]]] = []
        if mode == "file":
            attempts = [
                ("file", {"file": audio_data, "name": filename}),
                ("record", {"file": audio_data}),
                ("voice", {"file": audio_data}),
            ]
        elif mode == "text":
            attempts = []
        else:  # record 默认
            attempts = [
                ("record", {"file": audio_data}),
                ("voice", {"file": audio_data}),
                ("file", {"file": audio_data, "name": filename}),
            ]

        for custom_type, data in attempts:
            try:
                ok = await self.ctx.send.custom(custom_type, data, stream_id)
                if ok:
                    self.ctx.logger.info("音频已通过 %s 发送", custom_type)
                    return True
                self.ctx.logger.warning("发送 %s 返回 False", custom_type)
            except Exception as e:
                self.ctx.logger.warning("发送 %s 失败：%s", custom_type, e)

        # 文本兜底
        try:
            await self.ctx.send.text(
                f"🎵 已生成歌曲：{filename}，已保存到输出目录",
                stream_id,
            )
            return True
        except Exception as e:
            self.ctx.logger.error("文本兜底发送也失败：%s", e)
            return False

    def _match_genre_by_theme(self, theme: str) -> list[tuple[str, str, str, str, str]]:
        """根据主题关键词匹配音乐风格，返回候选列表。

        每个元素为 (genre, mood, instruments, tempo, scene)。
        调用方负责反单调选择（与 _last_genre 不同者优先）。
        """
        theme_text = (theme or "").lower()

        if any(k in theme_text for k in ["鼓励", "激励", "encourage", "加油", "cheer"]):
            candidates = [
                ("synth-pop", "uplifting", "synths and drums", "120 BPM", "bright morning"),
                ("funk", "energetic", "bass and brass", "110 BPM", "sunny street"),
                ("indie rock", "driving", "electric guitars", "130 BPM", "open highway"),
            ]
        elif any(k in theme_text for k in ["思念", "等待", "miss", "想念", "longing"]):
            candidates = [
                ("folk", "melancholic", "acoustic guitar", "72 BPM", "rainy window"),
                ("R&B", "soulful", "smooth keys and bass", "80 BPM", "quiet room"),
                ("lo-fi", "wistful", "mellow piano and vinyl crackle", "75 BPM", "late night desk"),
            ]
        elif any(k in theme_text for k in ["深夜", "安静", "night", "silent", "calm", "midnight"]):
            candidates = [
                ("ambient", "calm", "piano and pads", "60 BPM", "moonlit room"),
                ("lo-fi", "peaceful", "soft piano and vinyl crackle", "70 BPM", "quiet bedroom"),
                ("neoclassical", "serene", "strings and piano", "65 BPM", "candlelit hall"),
            ]
        elif any(k in theme_text for k in ["庆祝", "成就", "celebrate", "success", "party"]):
            candidates = [
                ("EDM", "euphoric", "electronic beats and synths", "128 BPM", "festival stage"),
                ("future bass", "joyful", "bright synths and heavy bass", "120 BPM", "celebration hall"),
                ("K-pop", "energetic", "synths and punchy drums", "125 BPM", "concert stage"),
            ]
        elif any(k in theme_text for k in ["吐槽", "抱怨", "complain", "rant"]):
            candidates = [
                ("funk", "sassy", "bass and brass", "100 BPM", "busy street"),
                ("rap", "sharp", "beats and 808s", "95 BPM", "underground venue"),
            ]
        else:
            # 日常 / 无法识别
            candidates = [
                ("city pop", "relaxed", "synth and guitar", "90 BPM", "city stroll"),
                ("bossa nova", "easygoing", "nylon guitar and light percussion", "85 BPM", "cafe terrace"),
            ]

        return candidates

    def _choose_genre(
        self, candidates: list[tuple[str, str, str, str, str]]
    ) -> tuple[str, str, str, str, str]:
        """从候选中选择一个，应用反单调规则（与 _last_genre 不同者优先）。"""
        chosen = candidates[0]
        for c in candidates:
            if c[0] != self._last_genre:
                chosen = c
                break
        self._last_genre = chosen[0]
        return chosen

    def _fallback_theme_by_personality(self, personality: str) -> str:
        """theme 为空时根据人格特征选主题。"""
        text = (personality or "").lower()
        if any(k in text for k in ["安静", "沉默", "话少", "quiet", "silent", "calm"]):
            return "午夜安眠曲"
        if any(k in text for k in ["活泼", "energetic", "lively", "cheerful", "俏皮"]):
            return "冒险歌"
        if any(k in text for k in ["神秘", "mysterious", "冷", "cool"]):
            return "月光小夜曲"
        return "日常随想"

    # ------------------------------------------------------------------
    # @Tool: generate_song
    # ------------------------------------------------------------------

    @Tool(
        "generate_song",
        brief_description="使用 MiniMax Music API 生成一首歌曲（人声或纯音乐）",
        detailed_description=(
            "生成音乐。mode='vocal' 生成带歌词的人声歌曲（可自定义 lyrics 或自动生成）；"
            "mode='instrumental' 生成纯音乐。prompt 用英文描述音乐风格效果最佳。\n"
            "参数 genre/mood/vocals/instruments/bpm 可选，用于精细控制。\n\n"
            "必填参数：prompt（音乐风格描述，建议英文）。\n"
            "可选参数：\n"
            "- mode：vocal（人声，默认）或 instrumental（纯音乐）\n"
            "- lyrics：自定义歌词，提供时直接使用不自动生成\n"
            "- auto_lyrics：mode=vocal 且无 lyrics 时是否自动生成歌词（默认 true）\n"
            "- genre/mood/vocals/instruments/bpm：结构化风格控制，会拼接到 prompt\n"
            "- msg_id：当前消息 ID"
        ),
        parameters=[
            ToolParameterInfo(
                name="prompt",
                param_type=ToolParamType.STRING,
                description="音乐风格描述，建议用英文（如 'indie folk, melancholic, acoustic guitar'）",
                required=True,
            ),
            ToolParameterInfo(
                name="mode",
                param_type=ToolParamType.STRING,
                description="生成模式：vocal（人声歌曲，默认）或 instrumental（纯音乐）",
                required=False,
                default="vocal",
            ),
            ToolParameterInfo(
                name="lyrics",
                param_type=ToolParamType.STRING,
                description="自定义歌词，提供时直接使用（不启用自动生成）",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="auto_lyrics",
                param_type=ToolParamType.STRING,
                description="mode=vocal 且无 lyrics 时是否自动生成歌词（true/false，默认 true）",
                required=False,
                default="true",
            ),
            ToolParameterInfo(
                name="genre",
                param_type=ToolParamType.STRING,
                description="（可选）音乐流派，如 'folk'、'EDM'、'jazz'",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="mood",
                param_type=ToolParamType.STRING,
                description="（可选）情绪，如 'melancholic'、'uplifting'、'calm'",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="vocals",
                param_type=ToolParamType.STRING,
                description="（可选）人声描述，如 'warm female voice'、'deep male voice'",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="instruments",
                param_type=ToolParamType.STRING,
                description="（可选）乐器，如 'acoustic guitar and piano'",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="bpm",
                param_type=ToolParamType.STRING,
                description="（可选）节拍速度，如 '90 BPM'",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="当前消息 ID",
                required=False,
                default="",
            ),
        ],
    )
    async def generate_song(
        self,
        prompt: str,
        mode: str = "vocal",
        lyrics: str = "",
        auto_lyrics: bool = True,
        genre: str = "",
        mood: str = "",
        vocals: str = "",
        instruments: str = "",
        bpm: str = "",
        msg_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """生成歌曲工具。"""
        # 1. 启用检查
        if not self._enabled or not self.music_service:
            return {"success": False, "content": "插件未启用（API Key 未配置）"}

        if not prompt or not str(prompt).strip():
            return {"success": False, "content": "prompt 不能为空"}

        # 兼容 SDK 传 string 的情况
        auto_lyrics = _to_bool(auto_lyrics)
        mode = (mode or "").lower()

        # 2. 解析 stream_id
        stream_id = await self._find_stream_id(**kwargs)

        # 3. 拼接结构化参数到 prompt
        full_prompt = str(prompt)
        extras = []
        if genre:
            extras.append(f"Genre: {genre}")
        if mood:
            extras.append(f"Mood: {mood}")
        if vocals:
            extras.append(f"Vocals: {vocals}")
        if instruments:
            extras.append(f"Instruments: {instruments}")
        if bpm:
            extras.append(f"BPM: {bpm}")
        if extras:
            full_prompt = f"{full_prompt}. {', '.join(extras)}"

        # 4. is_instrumental
        is_instrumental = (mode == "instrumental")

        # 5. lyrics 逻辑
        if is_instrumental:
            lyrics_param = None
            lyrics_optimizer = False
        elif lyrics:
            lyrics_param = lyrics
            lyrics_optimizer = False
        else:
            lyrics_param = None
            lyrics_optimizer = auto_lyrics

        # 6. 调用 music_service.generate
        music_cfg = self.config.music
        result = await self.music_service.generate(
            prompt=full_prompt,
            lyrics=lyrics_param,
            is_instrumental=is_instrumental,
            lyrics_optimizer=lyrics_optimizer,
            sample_rate=music_cfg.sample_rate,
            bitrate=music_cfg.bitrate,
            fmt=music_cfg.audio_format,
        )

        # 7. 成功
        if result.get("success"):
            audio_base64 = result.get("audio_base64", "")
            try:
                audio_bytes = base64.b64decode(audio_base64)
            except Exception as e:
                self.ctx.logger.error("base64 解码失败：%s", e)
                return {"success": False, "content": "生成失败：音频解码异常"}

            file_path = self._save_audio(audio_bytes, str(prompt))
            if file_path:
                filename = file_path.name
                file_path_str = str(file_path)
            else:
                filename = f"music_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
                file_path_str = ""

            # 发送
            if stream_id:
                await self._send_audio(audio_base64, filename, stream_id)
                content = f"已生成歌曲：{filename}，时长 {_format_duration(result.get('duration', 0))}"
            else:
                content = f"已生成并保存到 {file_path_str or filename}，未发送（无可用 stream_id）"

            return {
                "success": True,
                "content": content,
                "file_path": file_path_str,
            }

        # 8. 失败
        error = result.get("error", "未知错误")
        self.ctx.logger.warning("generate_song 失败：%s (code=%s)", error, result.get("code"))
        return {
            "success": False,
            "content": f"生成失败：{error}，请稍后重试或检查配置",
        }

    # ------------------------------------------------------------------
    # @Tool: buddy_sings
    # ------------------------------------------------------------------

    @Tool(
        "buddy_sings",
        brief_description="让 bot 用独特嗓音唱一首歌给用户",
        detailed_description=(
            "基于 bot 人格构建专属声音身份，根据 theme 匹配音乐风格，"
            "生成 bot 第一人称视角的歌曲。\n\n"
            "可选参数：\n"
            "- theme：歌曲主题（如 '今天的工作'、'深夜思念'、'鼓励'），不传则根据人格随机选主题\n"
            "- custom_lyrics：自定义歌词（应为 bot 第一人称），提供时不自动生成\n"
            "- regenerate：重新构建声音身份（true/false，默认 false）\n"
            "- msg_id：当前消息 ID"
        ),
        parameters=[
            ToolParameterInfo(
                name="theme",
                param_type=ToolParamType.STRING,
                description="（可选）歌曲主题，如 '今天的工作'、'深夜思念'、'鼓励'、'庆祝'",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="custom_lyrics",
                param_type=ToolParamType.STRING,
                description="（可选）自定义歌词，应为 bot 第一人称视角",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="regenerate",
                param_type=ToolParamType.STRING,
                description="（可选）是否重新构建声音身份（true/false，默认 false）",
                required=False,
                default="false",
            ),
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="当前消息 ID",
                required=False,
                default="",
            ),
        ],
    )
    async def buddy_sings(
        self,
        theme: str = "",
        custom_lyrics: str = "",
        regenerate: bool = False,
        msg_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """buddy_sings 工具：让 bot 唱歌。"""
        # 1. 启用检查
        if not self._enabled or not self.music_service:
            return {"success": False, "content": "插件未启用（API Key 未配置）"}

        # 2. buddy 功能检查
        if not self.config.buddy.enabled:
            return {"success": False, "content": "buddy_sings 功能未启用"}

        if not self.voice_mgr:
            return {"success": False, "content": "声音身份管理器未初始化"}

        regenerate = _to_bool(regenerate)

        # 3. 解析 stream_id
        stream_id = await self._find_stream_id(**kwargs)

        # 4. 获取 bot 人格
        buddy_cfg = self.config.buddy
        nickname = self._bot_nickname or buddy_cfg.fallback_nickname
        personality = self._bot_personality or buddy_cfg.fallback_personality

        # 5. regenerate → 删除缓存
        if regenerate:
            self.voice_mgr.regenerate(nickname)

        # 6. 获取/构建声音身份
        prompt_fragment = self.voice_mgr.get_or_build(
            nickname, personality, buddy_cfg.default_language
        )

        # 7. 主题 fallback
        if not theme:
            theme = self._fallback_theme_by_personality(personality)

        # 8. 匹配 genre + 反单调
        candidates = self._match_genre_by_theme(theme)
        genre, mood, instruments, tempo, scene = self._choose_genre(candidates)

        # 9. 拼接 prompt
        full_prompt = (
            f"{prompt_fragment}. A {genre} song with {mood} mood, "
            f"featuring {instruments}, at {tempo} tempo, evoking {scene}."
        )

        # 10. lyrics 逻辑
        if custom_lyrics:
            lyrics_param = custom_lyrics
            lyrics_optimizer = False
        else:
            lyrics_param = None
            lyrics_optimizer = True

        # 11. 调用 music_service.generate
        music_cfg = self.config.music
        result = await self.music_service.generate(
            prompt=full_prompt,
            lyrics=lyrics_param,
            is_instrumental=False,
            lyrics_optimizer=lyrics_optimizer,
            sample_rate=music_cfg.sample_rate,
            bitrate=music_cfg.bitrate,
            fmt=music_cfg.audio_format,
        )

        # 12. 成功
        if result.get("success"):
            audio_base64 = result.get("audio_base64", "")
            try:
                audio_bytes = base64.b64decode(audio_base64)
            except Exception as e:
                self.ctx.logger.error("base64 解码失败：%s", e)
                return {"success": False, "content": "生成失败：音频解码异常"}

            file_path = self._save_audio(audio_bytes, theme, is_buddy=True)
            if file_path:
                filename = file_path.name
                file_path_str = str(file_path)
            else:
                filename = f"{nickname}_sings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
                file_path_str = ""

            if stream_id:
                await self._send_audio(audio_base64, filename, stream_id)

            return {
                "success": True,
                "content": f"已为你唱了一首关于{theme}的{genre}歌曲",
                "theme": theme,
                "genre": genre,
                "file_path": file_path_str,
            }

        # 13. 失败
        error = result.get("error", "未知错误")
        self.ctx.logger.warning("buddy_sings 失败：%s (code=%s)", error, result.get("code"))
        return {
            "success": False,
            "content": f"生成失败：{error}，请稍后重试或检查配置",
        }

    # ------------------------------------------------------------------
    # @API: minimax_music_generate
    # ------------------------------------------------------------------

    @API(
        "minimax_music_generate",
        description="MiniMax 音乐生成",
        version="1",
        public=True,
    )
    async def minimax_music_generate(
        self,
        prompt: str,
        lyrics: str = "",
        is_instrumental: bool = False,
        lyrics_optimizer: bool = False,
        sample_rate: int = 44100,
        bitrate: int = 256000,
        fmt: str = "mp3",
    ) -> dict[str, Any]:
        """MiniMax 音乐生成 API（供其他插件程序化调用）。

        不自动发送消息、不保存文件，由调用方决定。
        """
        if not self._enabled or not self.music_service:
            return {"success": False, "error": "插件未启用"}

        result = await self.music_service.generate(
            prompt=prompt,
            lyrics=lyrics or None,
            is_instrumental=is_instrumental,
            lyrics_optimizer=lyrics_optimizer,
            sample_rate=sample_rate,
            bitrate=bitrate,
            fmt=fmt,
        )
        return result


# ====================================================================
# 工厂函数
# ====================================================================


def create_plugin() -> MiniMaxMusicPlugin:
    """工厂函数，返回插件实例。"""
    return MiniMaxMusicPlugin()