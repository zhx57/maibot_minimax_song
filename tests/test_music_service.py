"""MiniMaxMusicService 单元测试。

所有 HTTP 调用通过内存会话桩 mock，不发起真实网络请求。
覆盖：payload 构造、hex 解码、错误码分类、重试逻辑、文本长度校验、
instrumental 模型校验、翻唱流程、配置热重载。
"""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from music_service import MiniMaxMusicService

API_BASE = "https://api.minimaxi.com"
GEN_URL = f"{API_BASE}/v1/music_generation"
PREPROCESS_URL = f"{API_BASE}/v1/music_cover_preprocess"
LYRICS_URL = f"{API_BASE}/v1/lyrics_generation"


@pytest.fixture
def service(mock_logger):
    """默认使用 music-2.6-free 模型的服务实例。"""
    return MiniMaxMusicService(
        api_key="test-key",
        api_base_url=API_BASE,
        model="music-2.6-free",
        max_retries=3,
        retry_backoff_base=1.5,
        logger=mock_logger,
    )


class _MockResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, content_type=None):
        return self._payload


class _MockSession:
    def __init__(self, *results):
        self._results = list(results)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(SimpleNamespace(url=url, kwargs=kwargs))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return _MockResponse(result)


def _mock_http(service, *results):
    session = _MockSession(*results)
    service._get_session = AsyncMock(return_value=session)
    return session


# ----------------------------------------------------------------------
# 成功与 payload 构造
# ----------------------------------------------------------------------

async def test_generate_success(service, sample_api_response_success, sample_audio_bytes):
    """成功生成：验证 payload 含 output_format=hex/audio_setting，返回 base64 正确。"""
    session = _mock_http(service, sample_api_response_success)
    result = await service.generate(
        prompt="indie folk, melancholic",
        lyrics="[verse]\nhello",
        is_instrumental=False,
    )

    assert result["success"] is True
    expected_b64 = base64.b64encode(sample_audio_bytes).decode("ascii")
    assert result["audio_base64"] == expected_b64
    assert result["format"] == "mp3"
    assert result["duration"] == 30000
    assert result["size"] == len(sample_audio_bytes)

    calls = session.requests
    assert len(calls) == 1
    assert calls[0].url == GEN_URL
    payload = calls[0].kwargs["json"]
    assert payload["output_format"] == "hex"
    assert payload["is_instrumental"] is False
    assert payload["model"] == "music-2.6-free"
    assert payload["audio_setting"]["sample_rate"] == 44100
    assert payload["audio_setting"]["bitrate"] == 256000
    assert payload["audio_setting"]["format"] == "mp3"
    assert "channel" not in payload["audio_setting"]
    headers = calls[0].kwargs["headers"]
    assert headers["Authorization"] == "Bearer test-key"
    assert headers["Content-Type"] == "application/json"


async def test_generate_instrumental_payload(service, sample_api_response_success):
    """is_instrumental=True 时 payload 不含 lyrics。"""
    session = _mock_http(service, sample_api_response_success)
    await service.generate(prompt="jazz piano", is_instrumental=True)

    payload = session.requests[0].kwargs["json"]
    assert payload["is_instrumental"] is True
    assert "lyrics" not in payload
    assert "lyrics_optimizer" not in payload


async def test_generate_with_lyrics(service, sample_api_response_success):
    """传 lyrics 时 payload 含 lyrics，不含 lyrics_optimizer。"""
    session = _mock_http(service, sample_api_response_success)
    await service.generate(
        prompt="folk", lyrics="[verse]\nla la la", is_instrumental=False
    )

    payload = session.requests[0].kwargs["json"]
    assert payload["lyrics"] == "[verse]\nla la la"
    assert "lyrics_optimizer" not in payload


async def test_generate_auto_lyrics(service, sample_api_response_success):
    """lyrics=None + lyrics_optimizer=True 时 payload 含 lyrics_optimizer=true。"""
    session = _mock_http(service, sample_api_response_success)
    await service.generate(prompt="folk", lyrics=None, lyrics_optimizer=True)

    payload = session.requests[0].kwargs["json"]
    assert payload.get("lyrics_optimizer") is True
    assert "lyrics" not in payload


# ----------------------------------------------------------------------
# 文本长度校验
# ----------------------------------------------------------------------

async def test_generate_prompt_too_long(service):
    """prompt 超 1500 字符返回 success=False，不发起 HTTP 请求。"""
    result = await service.generate(prompt="x" * 1501)
    assert result["success"] is False
    assert "1500" in result["error"]
    assert service._session is None


async def test_generate_lyrics_too_long(service):
    """lyrics 超 1500 字符返回 success=False。"""
    result = await service.generate(prompt="folk", lyrics="y" * 1501)
    assert result["success"] is False
    assert "lyrics" in result["error"]
    assert service._session is None


# ----------------------------------------------------------------------
# instrumental 模型校验
# ----------------------------------------------------------------------

async def test_generate_instrumental_unsupported_model(mock_logger):
    """翻唱模型不能用于普通音乐生成。"""
    svc = MiniMaxMusicService(api_key="k", model="music-cover", logger=mock_logger)
    result = await svc.generate(prompt="piano", is_instrumental=True)
    assert result["success"] is False
    assert "generation" in result["error"]
    assert svc._session is None


# ----------------------------------------------------------------------
# 错误码分类与重试
# ----------------------------------------------------------------------

async def test_generate_retryable_error_retries(
    service, sample_api_response_success, fast_sleep
):
    """status_code=1002（限流）第一次，第二次成功 → 验证重试发生。"""
    err = {"base_resp": {"status_code": 1002, "status_msg": "rate limit"}, "data": {}}
    session = _mock_http(service, err, sample_api_response_success)
    result = await service.generate(prompt="folk")

    assert result["success"] is True
    assert len(session.requests) == 2


async def test_generate_fatal_error_no_retry(service, fast_sleep):
    """status_code=1004（鉴权）→ fatal，不重试，立即返回。"""
    err = {"base_resp": {"status_code": 1004, "status_msg": "auth fail"}, "data": {}}
    session = _mock_http(service, err)
    result = await service.generate(prompt="folk")

    assert result["success"] is False
    assert result["code"] == 1004
    # fatal 不重试：仅一次请求
    assert len(session.requests) == 1


async def test_generate_network_error_retries(
    service, sample_api_response_success, fast_sleep
):
    """aiohttp.ClientError 第一次，第二次成功 → 验证重试。"""
    session = _mock_http(
        service, aiohttp.ClientError("boom"), sample_api_response_success
    )
    result = await service.generate(prompt="folk")

    assert result["success"] is True
    assert len(session.requests) == 2


async def test_generate_retries_exhausted(service, fast_sleep):
    """始终返回 1002 → 重试耗尽返回 success=False。"""
    err = {"base_resp": {"status_code": 1002, "status_msg": "rate limit"}, "data": {}}
    session = _mock_http(service, err, err, err, err)
    result = await service.generate(prompt="folk")

    assert result["success"] is False
    # max_retries=3 → 4 次尝试
    assert len(session.requests) == 4


# ----------------------------------------------------------------------
# 翻唱生成
# ----------------------------------------------------------------------

async def test_cover_uses_music_generation(service, sample_api_response_success):
    """翻唱与生成共用 music_generation，并传翻唱模型和音频 URL。"""
    session = _mock_http(service, sample_api_response_success)
    result = await service.cover(
        prompt="acoustic cover", audio_url="https://example.com/ref.mp3"
    )

    assert result["success"] is True
    assert result["audio_base64"] == base64.b64encode(b"hello").decode()
    cover_calls = session.requests
    assert len(cover_calls) == 1
    cover_payload = cover_calls[0].kwargs["json"]
    assert cover_payload["model"] == "music-cover-free"
    assert cover_payload["audio_url"] == "https://example.com/ref.mp3"
    assert cover_payload["prompt"] == "acoustic cover"
    assert cover_payload["output_format"] == "hex"


async def test_cover_missing_audio(service):
    """未提供任何音频来源时返回错误。"""
    result = await service.cover(prompt="acoustic cover")
    assert result["success"] is False
    assert "audio_url" in result["error"]
    assert service._session is None


# ----------------------------------------------------------------------
# 错误码分类
# ----------------------------------------------------------------------

def test_classify_error(service):
    """验证 1002→retryable，1004/1008/1026/2013→fatal，未知→fatal。"""
    assert service._classify_error(1002) == "retryable"
    for code in (1004, 1008, 1026, 2013, 2049):
        assert service._classify_error(code) == "fatal"
    # 未知码归 fatal
    assert service._classify_error(9999) == "fatal"
    assert service._classify_error(-1) == "fatal"


# ----------------------------------------------------------------------
# 配置热重载
# ----------------------------------------------------------------------

def test_update_methods(service):
    """update_api_key/update_api_base_url/update_model 更新配置。"""
    service.update_api_key("new-key")
    assert service._api_key == "new-key"

    service.update_api_base_url("https://api.minimax.io/")
    assert service._api_base_url == "https://api.minimax.io"

    service.update_model("music-2.6")
    assert service._model == "music-2.6"
    service.update_output_format("url")
    assert service._output_format == "url"


async def test_cover_preprocess(service):
    response = {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "cover_feature_id": "feature-1",
        "formatted_lyrics": "[Verse]\nhello world",
        "audio_duration": 90,
    }
    session = _mock_http(service, response)
    result = await service.cover_preprocess(audio_url="https://example.com/ref.mp3")
    assert result["success"] is True
    assert result["cover_feature_id"] == "feature-1"
    assert session.requests[0].url == PREPROCESS_URL
    payload = session.requests[0].kwargs["json"]
    assert payload == {"model": "music-cover", "audio_url": "https://example.com/ref.mp3"}


async def test_generate_lyrics(service):
    response = {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "lyrics": "[Verse]\nsummer wind",
        "song_title": "Summer",
    }
    session = _mock_http(service, response)
    result = await service.generate_lyrics(
        "write_full_song", prompt="summer", title="Summer"
    )
    assert result["success"] is True
    assert result["lyrics"] == "[Verse]\nsummer wind"
    assert session.requests[0].url == LYRICS_URL
    assert session.requests[0].kwargs["json"] == {
        "mode": "write_full_song",
        "prompt": "summer",
        "title": "Summer",
    }
