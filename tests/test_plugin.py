"""MiniMaxMusicPlugin 单元测试。

mock ctx 与 music_service / voice_mgr，不真正调用 MiniMax API。
覆盖：生命周期（on_load/on_unload/on_config_update）、工具调用、
stream_id 解析、_send_audio 回退链、_save_audio 文件名、
_match_genre_by_theme 映射。
"""
import base64
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

import plugin as plugin_mod
from plugin import (
    CONFIG_RELOAD_SCOPE_SELF,
    ON_BOT_CONFIG_RELOAD,
    MiniMaxMusicPlugin,
    MusicPluginConfig,
    create_plugin,
)


# ----------------------------------------------------------------------
# 公共 fixture
# ----------------------------------------------------------------------

@pytest.fixture
def plugin(mock_ctx):
    """带 mock ctx 与默认 config 的插件实例（未初始化 service）。"""
    p = create_plugin()
    p.ctx = mock_ctx
    p.config = MusicPluginConfig()
    return p


@pytest.fixture
def plugin_with_services(plugin):
    """已注入 mock music_service 与 voice_mgr 的插件。"""
    plugin.music_service = MagicMock()
    plugin.music_service.generate = AsyncMock()
    plugin.music_service.close = AsyncMock()
    plugin.voice_mgr = MagicMock()
    plugin.voice_mgr.get_or_build = MagicMock(return_value="Vocal: warm voice.")
    plugin.voice_mgr.regenerate = MagicMock()
    plugin.voice_mgr.close = MagicMock()
    plugin._enabled = True
    return plugin


def _success_result():
    """music_service.generate 的成功返回。"""
    return {
        "success": True,
        "audio_base64": base64.b64encode(b"hello").decode(),
        "format": "mp3",
        "duration": 30000,
        "size": 5,
    }


# ----------------------------------------------------------------------
# 生命周期：on_load
# ----------------------------------------------------------------------

async def test_on_load_initializes_services(plugin, monkeypatch):
    """api_key 非空时，music_service 和 voice_mgr 被初始化。"""
    # 屏蔽 _ensure_config_exists 避免触碰真实插件目录
    monkeypatch.setattr(MiniMaxMusicPlugin, "_ensure_config_exists", lambda self: None)
    plugin.config.music.minimax_api_key = "test-key"
    await plugin.on_load()
    assert plugin._enabled is True
    assert plugin.music_service is not None
    assert plugin.voice_mgr is not None
    # fallback 昵称
    assert plugin._bot_nickname == "麦麦"


async def test_on_load_disables_when_no_api_key(plugin, monkeypatch):
    """api_key 空时 _enabled=False。"""
    monkeypatch.setattr(MiniMaxMusicPlugin, "_ensure_config_exists", lambda self: None)
    plugin.config.music.minimax_api_key = ""
    await plugin.on_load()
    assert plugin._enabled is False
    assert plugin.music_service is None


def test_on_load_copies_config_toml(mock_ctx, tmp_path, monkeypatch):
    """config.toml 不存在时从 config.example.toml 复制。"""
    # 构造一个临时"插件目录"：有 config.example.toml，无 config.toml
    fake_plugin_file = tmp_path / "fake_plugin.py"
    fake_plugin_file.write_text("# fake")
    (tmp_path / "config.example.toml").write_text("[plugin]\nenabled = true\n")

    # 让 _ensure_config_exists 内的 Path(__file__).parent 指向临时目录
    monkeypatch.setattr(plugin_mod, "__file__", str(fake_plugin_file))

    p = create_plugin()
    p.ctx = mock_ctx
    p._ensure_config_exists()

    assert (tmp_path / "config.toml").exists()
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == "[plugin]\nenabled = true\n"


# ----------------------------------------------------------------------
# 生命周期：on_unload
# ----------------------------------------------------------------------

async def test_on_unload_closes_services(plugin_with_services):
    """on_unload 调用 music_service.close() 与 voice_mgr.close()。"""
    music_service = plugin_with_services.music_service
    voice_mgr = plugin_with_services.voice_mgr
    await plugin_with_services.on_unload()
    music_service.close.assert_awaited_once()
    voice_mgr.close.assert_called_once()
    assert plugin_with_services.music_service is None
    assert plugin_with_services.voice_mgr is None
    assert plugin_with_services._enabled is False


# ----------------------------------------------------------------------
# 生命周期：on_config_update
# ----------------------------------------------------------------------

async def test_on_config_update_self_scope(plugin_with_services):
    """scope=CONFIG_RELOAD_SCOPE_SELF 时更新 service 配置。"""
    plugin_with_services.config.music.minimax_api_key = "new-key"
    plugin_with_services.config.music.api_base_url = "https://api.minimax.io"
    plugin_with_services.config.music.model = "music-2.5+"
    await plugin_with_services.on_config_update(
        scope=CONFIG_RELOAD_SCOPE_SELF, config_data={}, version="2"
    )
    plugin_with_services.music_service.update_api_key.assert_called_once_with("new-key")
    plugin_with_services.music_service.update_api_base_url.assert_called_once_with(
        "https://api.minimax.io"
    )
    plugin_with_services.music_service.update_model.assert_called_once_with("music-2.5+")
    assert plugin_with_services._enabled is True


async def test_on_config_update_bot_scope_extracts_nickname(plugin_with_services):
    """scope=bot，config_data={'bot':{'nickname':'新名字'}} → _bot_nickname 更新。"""
    await plugin_with_services.on_config_update(
        scope=ON_BOT_CONFIG_RELOAD,
        config_data={"bot": {"nickname": "新名字"}},
        version="1",
    )
    assert plugin_with_services._bot_nickname == "新名字"


async def test_on_config_update_bot_scope_flat_fallback(plugin_with_services):
    """config_data={'nickname':'扁平名'} → _bot_nickname 更新（防御性解析）。"""
    await plugin_with_services.on_config_update(
        scope="bot",
        config_data={"nickname": "扁平名"},
        version="1",
    )
    assert plugin_with_services._bot_nickname == "扁平名"


async def test_on_config_update_bot_scope_personality(plugin_with_services):
    """验证 _bot_personality 更新。"""
    await plugin_with_services.on_config_update(
        scope=ON_BOT_CONFIG_RELOAD,
        config_data={"personality": {"personality": "神秘"}},
        version="1",
    )
    assert plugin_with_services._bot_personality == "神秘"


# ----------------------------------------------------------------------
# generate_song 工具
# ----------------------------------------------------------------------

async def test_generate_song_vocal_success(plugin_with_services, mock_ctx):
    """mock music_service.generate 成功，验证 _save_audio + _send_audio 调用，返回 success=True。"""
    plugin_with_services.music_service.generate = AsyncMock(return_value=_success_result())
    result = await plugin_with_services.generate_song(
        prompt="indie folk", mode="vocal", stream_id="s1"
    )
    assert result["success"] is True
    assert "已生成歌曲" in result["content"]
    plugin_with_services.music_service.generate.assert_awaited_once()
    # _send_audio 触发了 ctx.send.custom
    mock_ctx.send.custom.assert_awaited()


async def test_generate_song_instrumental(plugin_with_services):
    """mode=instrumental 时 is_instrumental=True。"""
    plugin_with_services.music_service.generate = AsyncMock(return_value=_success_result())
    await plugin_with_services.generate_song(
        prompt="jazz", mode="instrumental", stream_id="s1"
    )
    kwargs = plugin_with_services.music_service.generate.await_args.kwargs
    assert kwargs["is_instrumental"] is True
    assert kwargs["lyrics"] is None
    assert kwargs["lyrics_optimizer"] is False


async def test_generate_song_auto_lyrics(plugin_with_services):
    """无 lyrics + auto_lyrics=True 时 lyrics_optimizer=True。"""
    plugin_with_services.music_service.generate = AsyncMock(return_value=_success_result())
    await plugin_with_services.generate_song(
        prompt="folk", mode="vocal", stream_id="s1"
    )
    kwargs = plugin_with_services.music_service.generate.await_args.kwargs
    assert kwargs["lyrics_optimizer"] is True
    assert kwargs["lyrics"] is None


async def test_generate_song_failure(plugin_with_services, mock_ctx):
    """music_service.generate 失败，返回 success=False，不调用 _send_audio。"""
    plugin_with_services.music_service.generate = AsyncMock(
        return_value={"success": False, "error": "auth fail", "code": 1004}
    )
    result = await plugin_with_services.generate_song(prompt="folk", stream_id="s1")
    assert result["success"] is False
    assert "生成失败" in result["content"]
    mock_ctx.send.custom.assert_not_awaited()


async def test_generate_song_no_stream_id(plugin_with_services, mock_ctx):
    """无 stream_id 时仅保存返回成功。"""
    plugin_with_services.music_service.generate = AsyncMock(return_value=_success_result())
    # 无 stream_id 且无 message 且 group/private streams 为空（mock_ctx 默认）
    result = await plugin_with_services.generate_song(prompt="folk")
    assert result["success"] is True
    assert "未发送" in result["content"]
    mock_ctx.send.custom.assert_not_awaited()


# ----------------------------------------------------------------------
# cover_song 工具
# ----------------------------------------------------------------------

async def test_cover_song_success(plugin_with_services, mock_ctx):
    """mock music_service.cover 成功，验证 audio_url 透传 + _send_audio 调用。"""
    plugin_with_services.music_service.cover = AsyncMock(return_value=_success_result())
    result = await plugin_with_services.cover_song(
        prompt="acoustic cover with soft piano",
        audio_url="https://example.com/song.mp3",
        stream_id="s1",
    )
    assert result["success"] is True
    assert "已生成翻唱" in result["content"]
    # cover 被调用且 audio_url 透传
    plugin_with_services.music_service.cover.assert_awaited_once()
    kwargs = plugin_with_services.music_service.cover.await_args.kwargs
    assert kwargs["audio_url"] == "https://example.com/song.mp3"
    assert kwargs["prompt"] == "acoustic cover with soft piano"
    # _send_audio 触发
    mock_ctx.send.custom.assert_awaited()


async def test_cover_song_failure(plugin_with_services, mock_ctx):
    """cover 失败时返回中文错误，不发送。"""
    plugin_with_services.music_service.cover = AsyncMock(
        return_value={"success": False, "error": "audio_url 不可访问", "code": -1}
    )
    result = await plugin_with_services.cover_song(
        prompt="cover", audio_url="https://example.com/x.mp3", stream_id="s1"
    )
    assert result["success"] is False
    assert "翻唱失败" in result["content"]
    mock_ctx.send.custom.assert_not_awaited()


async def test_cover_song_missing_audio_url(plugin_with_services):
    """audio_url 为空时返回错误，不调用 cover。"""
    plugin_with_services.music_service.cover = AsyncMock()
    result = await plugin_with_services.cover_song(prompt="cover", audio_url="")
    assert result["success"] is False
    assert "audio_url" in result["content"]
    plugin_with_services.music_service.cover.assert_not_awaited()


async def test_cover_song_with_custom_lyrics(plugin_with_services):
    """传 lyrics 时透传给 cover（非 None）。"""
    plugin_with_services.music_service.cover = AsyncMock(return_value=_success_result())
    await plugin_with_services.cover_song(
        prompt="cover",
        audio_url="https://example.com/s.mp3",
        lyrics="[verse]\nnew lyrics",
        stream_id="s1",
    )
    kwargs = plugin_with_services.music_service.cover.await_args.kwargs
    assert kwargs["lyrics"] == "[verse]\nnew lyrics"


# ----------------------------------------------------------------------
# buddy_sings 工具
# ----------------------------------------------------------------------

async def test_buddy_sings_success(plugin_with_services):
    """mock voice_mgr.get_or_build + music_service.generate 成功。"""
    plugin_with_services.music_service.generate = AsyncMock(return_value=_success_result())
    plugin_with_services._bot_nickname = "麦麦"
    plugin_with_services._bot_personality = "活泼"
    result = await plugin_with_services.buddy_sings(theme="今天的工作", stream_id="s1")
    assert result["success"] is True
    assert "genre" in result
    assert result["theme"] == "今天的工作"
    plugin_with_services.voice_mgr.get_or_build.assert_called_once()


async def test_buddy_sings_regenerate(plugin_with_services):
    """regenerate=True 时调用 voice_mgr.regenerate。"""
    plugin_with_services.music_service.generate = AsyncMock(return_value=_success_result())
    await plugin_with_services.buddy_sings(theme="test", regenerate=True, stream_id="s1")
    plugin_with_services.voice_mgr.regenerate.assert_called_once()


async def test_buddy_sings_anti_monotone(plugin_with_services):
    """连续两次调用，验证 genre 不同。"""
    plugin_with_services.music_service.generate = AsyncMock(return_value=_success_result())
    r1 = await plugin_with_services.buddy_sings(theme="鼓励", stream_id="s1")
    r2 = await plugin_with_services.buddy_sings(theme="鼓励", stream_id="s1")
    assert r1["genre"] != r2["genre"]


async def test_buddy_sings_theme_matching(plugin_with_services):
    """theme='深夜' 时 genre 含 ambient 或 lo-fi（或 neoclassical）。"""
    plugin_with_services.music_service.generate = AsyncMock(return_value=_success_result())
    result = await plugin_with_services.buddy_sings(theme="深夜", stream_id="s1")
    assert result["genre"] in ("ambient", "lo-fi", "neoclassical")


async def test_buddy_sings_disabled(plugin_with_services):
    """buddy.enabled=False 时返回错误。"""
    plugin_with_services.config.buddy.enabled = False
    result = await plugin_with_services.buddy_sings(theme="test", stream_id="s1")
    assert result["success"] is False
    assert "未启用" in result["content"]


# ----------------------------------------------------------------------
# minimax_music_generate API
# ----------------------------------------------------------------------

async def test_api_call_success(plugin_with_services):
    """minimax_music_generate API 返回 music_service.generate 的结果。"""
    plugin_with_services.music_service.generate = AsyncMock(return_value=_success_result())
    result = await plugin_with_services.minimax_music_generate(prompt="folk")
    assert result["success"] is True
    assert result["audio_base64"] == base64.b64encode(b"hello").decode()


# ----------------------------------------------------------------------
# _find_stream_id
# ----------------------------------------------------------------------

async def test_find_stream_id_from_kwargs(plugin):
    """kwargs 含 stream_id。"""
    sid = await plugin._find_stream_id(stream_id="s1")
    assert sid == "s1"


async def test_find_stream_id_from_message(plugin):
    """kwargs.message.stream_id。"""
    msg = MagicMock()
    msg.stream_id = "s2"
    sid = await plugin._find_stream_id(message=msg)
    assert sid == "s2"


async def test_find_stream_id_from_group_streams(plugin, mock_ctx):
    """chat.get_group_streams 非空。"""
    mock_ctx.chat.get_group_streams = AsyncMock(return_value=[{"stream_id": "s3"}])
    sid = await plugin._find_stream_id()
    assert sid == "s3"


async def test_find_stream_id_returns_none(plugin, mock_ctx):
    """全都没有返回 None。"""
    mock_ctx.chat.get_group_streams = AsyncMock(return_value=[])
    mock_ctx.chat.get_private_streams = AsyncMock(return_value=[])
    sid = await plugin._find_stream_id()
    assert sid is None


# ----------------------------------------------------------------------
# _send_audio
# ----------------------------------------------------------------------

async def test_send_audio_record_mode(plugin_with_services, mock_ctx):
    """send_mode=record，ctx.send.custom('record', ...) 成功。"""
    plugin_with_services.config.music.send_mode = "record"
    ok = await plugin_with_services._send_audio("aGVsbG8=", "song.mp3", "s1")
    assert ok is True
    mock_ctx.send.custom.assert_awaited_once()
    args = mock_ctx.send.custom.await_args
    assert args.args[0] == "record"
    assert args.args[2] == "s1"
    assert args.args[1]["file"] == "base64://aGVsbG8="


async def test_send_audio_fallback_to_text(plugin_with_services, mock_ctx):
    """所有 custom 失败，回退 ctx.send.text。"""
    plugin_with_services.config.music.send_mode = "record"
    mock_ctx.send.custom = AsyncMock(return_value=False)
    ok = await plugin_with_services._send_audio("aGVsbG8=", "song.mp3", "s1")
    assert ok is True  # 文本兜底成功
    mock_ctx.send.text.assert_awaited_once()


# ----------------------------------------------------------------------
# _save_audio
# ----------------------------------------------------------------------

def test_save_audio_filename_format(plugin, mock_ctx):
    """普通模式 YYYYMMDD_HHMMSS_<slug>.mp3。"""
    path = plugin._save_audio(b"hello", "indie folk, melancholic tune", is_buddy=False)
    assert path is not None
    assert path.suffix == ".mp3"
    name = path.name
    assert re.match(r"^\d{8}_\d{6}_.+\.mp3$", name)
    # slug 来自 prompt 前 20 字符（移除特殊字符后）
    assert "indie" in name


def test_save_audio_buddy_filename(plugin, mock_ctx):
    """buddy 模式 <nickname>_sings_<ts>.mp3。"""
    plugin._bot_nickname = "麦麦"
    path = plugin._save_audio(b"hello", "深夜", is_buddy=True)
    assert path is not None
    name = path.name
    assert name.startswith("麦麦_sings_")
    assert name.endswith(".mp3")
    assert re.match(r"^麦麦_sings_\d{8}_\d{6}\.mp3$", name)


# ----------------------------------------------------------------------
# _match_genre_by_theme
# ----------------------------------------------------------------------

def test_match_genre_by_theme_encourage(plugin):
    """theme 含'鼓励'返回 synth-pop 或 funk（或 indie rock）。"""
    candidates = plugin._match_genre_by_theme("鼓励一下我")
    genres = [c[0] for c in candidates]
    assert "synth-pop" in genres
    assert "funk" in genres


def test_match_genre_by_theme_miss(plugin):
    """theme 含'思念'返回 folk 或 R&B（或 lo-fi）。"""
    candidates = plugin._match_genre_by_theme("思念远方的朋友")
    genres = [c[0] for c in candidates]
    assert "folk" in genres
    assert "R&B" in genres
