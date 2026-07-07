"""MiniMaxMusicService 单元测试。

所有 HTTP 调用通过 ``aioresponses`` mock，不发起真实网络请求。
覆盖：payload 构造、hex 解码、错误码分类、重试逻辑、文本长度校验、
instrumental 模型校验、翻唱流程、配置热重载。
"""
import base64

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from music_service import MiniMaxMusicService

API_BASE = "https://api.minimaxi.com"
GEN_URL = f"{API_BASE}/v1/music_generation"
COVER_URL = f"{API_BASE}/v1/music_cover"
UPLOAD_URL = f"{API_BASE}/v1/files/upload"


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


def _requests(m, method, url):
    """取出 aioresponses 记录的请求列表。"""
    return m.requests[(method, URL(url))]


# ----------------------------------------------------------------------
# 成功与 payload 构造
# ----------------------------------------------------------------------

async def test_generate_success(service, sample_api_response_success, sample_audio_bytes):
    """成功生成：验证 payload 含 output_format=hex/audio_setting，返回 base64 正确。"""
    with aioresponses() as m:
        m.post(GEN_URL, payload=sample_api_response_success)
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

    calls = _requests(m, "POST", GEN_URL)
    assert len(calls) == 1
    payload = calls[0].kwargs["json"]
    assert payload["output_format"] == "hex"
    assert payload["is_instrumental"] is False
    assert payload["model"] == "music-2.6-free"
    assert payload["audio_setting"]["sample_rate"] == 44100
    assert payload["audio_setting"]["bitrate"] == 256000
    assert payload["audio_setting"]["format"] == "mp3"
    assert payload["audio_setting"]["channel"] == 1
    headers = calls[0].kwargs["headers"]
    assert headers["Authorization"] == "Bearer test-key"
    assert headers["Content-Type"] == "application/json"


async def test_generate_instrumental_payload(service, sample_api_response_success):
    """is_instrumental=True 时 payload 不含 lyrics。"""
    with aioresponses() as m:
        m.post(GEN_URL, payload=sample_api_response_success)
        await service.generate(prompt="jazz piano", is_instrumental=True)

    payload = _requests(m, "POST", GEN_URL)[0].kwargs["json"]
    assert payload["is_instrumental"] is True
    assert "lyrics" not in payload
    assert "lyrics_optimizer" not in payload


async def test_generate_with_lyrics(service, sample_api_response_success):
    """传 lyrics 时 payload 含 lyrics，不含 lyrics_optimizer。"""
    with aioresponses() as m:
        m.post(GEN_URL, payload=sample_api_response_success)
        await service.generate(
            prompt="folk", lyrics="[verse]\nla la la", is_instrumental=False
        )

    payload = _requests(m, "POST", GEN_URL)[0].kwargs["json"]
    assert payload["lyrics"] == "[verse]\nla la la"
    assert "lyrics_optimizer" not in payload


async def test_generate_auto_lyrics(service, sample_api_response_success):
    """lyrics=None + lyrics_optimizer=True 时 payload 含 lyrics_optimizer=true。"""
    with aioresponses() as m:
        m.post(GEN_URL, payload=sample_api_response_success)
        await service.generate(prompt="folk", lyrics=None, lyrics_optimizer=True)

    payload = _requests(m, "POST", GEN_URL)[0].kwargs["json"]
    assert payload.get("lyrics_optimizer") is True
    assert "lyrics" not in payload


# ----------------------------------------------------------------------
# 文本长度校验
# ----------------------------------------------------------------------

async def test_generate_prompt_too_long(service):
    """prompt 超 2000 字符返回 success=False，不发起 HTTP 请求。"""
    with aioresponses() as m:
        result = await service.generate(prompt="x" * 2001)
    assert result["success"] is False
    assert "2000" in result["error"]
    assert len(m.requests) == 0


async def test_generate_lyrics_too_long(service):
    """lyrics 超 3500 字符返回 success=False。"""
    with aioresponses() as m:
        result = await service.generate(prompt="folk", lyrics="y" * 3501)
    assert result["success"] is False
    assert "lyrics" in result["error"]
    assert len(m.requests) == 0


# ----------------------------------------------------------------------
# instrumental 模型校验
# ----------------------------------------------------------------------

async def test_generate_instrumental_unsupported_model(mock_logger):
    """model=music-2.5 + is_instrumental=True 返回明确错误，不调用 API。"""
    svc = MiniMaxMusicService(api_key="k", model="music-2.5", logger=mock_logger)
    with aioresponses() as m:
        result = await svc.generate(prompt="piano", is_instrumental=True)
    assert result["success"] is False
    assert "instrumental" in result["error"]
    assert len(m.requests) == 0


# ----------------------------------------------------------------------
# 错误码分类与重试
# ----------------------------------------------------------------------

async def test_generate_retryable_error_retries(
    service, sample_api_response_success, fast_sleep
):
    """status_code=1002（限流）第一次，第二次成功 → 验证重试发生。"""
    err = {"base_resp": {"status_code": 1002, "status_msg": "rate limit"}, "data": {}}
    with aioresponses() as m:
        m.post(GEN_URL, payload=err)
        m.post(GEN_URL, payload=sample_api_response_success)
        result = await service.generate(prompt="folk")

    assert result["success"] is True
    assert len(_requests(m, "POST", GEN_URL)) == 2


async def test_generate_fatal_error_no_retry(service, fast_sleep):
    """status_code=1004（鉴权）→ fatal，不重试，立即返回。"""
    err = {"base_resp": {"status_code": 1004, "status_msg": "auth fail"}, "data": {}}
    with aioresponses() as m:
        m.post(GEN_URL, payload=err, repeat=True)
        result = await service.generate(prompt="folk")

    assert result["success"] is False
    assert result["code"] == 1004
    # fatal 不重试：仅一次请求
    assert len(_requests(m, "POST", GEN_URL)) == 1


async def test_generate_network_error_retries(
    service, sample_api_response_success, fast_sleep
):
    """aiohttp.ClientError 第一次，第二次成功 → 验证重试。"""
    with aioresponses() as m:
        m.post(GEN_URL, exception=aiohttp.ClientError("boom"))
        m.post(GEN_URL, payload=sample_api_response_success)
        result = await service.generate(prompt="folk")

    assert result["success"] is True
    assert len(_requests(m, "POST", GEN_URL)) == 2


async def test_generate_retries_exhausted(service, fast_sleep):
    """始终返回 1002 → 重试耗尽返回 success=False。"""
    err = {"base_resp": {"status_code": 1002, "status_msg": "rate limit"}, "data": {}}
    with aioresponses() as m:
        m.post(GEN_URL, payload=err, repeat=True)
        result = await service.generate(prompt="folk")

    assert result["success"] is False
    # max_retries=3 → 4 次尝试
    assert len(_requests(m, "POST", GEN_URL)) == 4


# ----------------------------------------------------------------------
# 翻唱生成
# ----------------------------------------------------------------------

async def test_cover_with_audio_file(
    service, sample_api_response_success, tmp_path, fast_sleep
):
    """上传参考音频到 /v1/files/upload + 调用 /v1/music_cover，验证流程。"""
    audio_file = tmp_path / "ref.mp3"
    audio_file.write_bytes(b"ref audio content")

    upload_resp = {
        "base_resp": {"status_code": 0, "status_msg": "ok"},
        "file": {"file_id": 12345, "filename": "ref.mp3"},
    }
    with aioresponses() as m:
        m.post(UPLOAD_URL, payload=upload_resp)
        m.post(COVER_URL, payload=sample_api_response_success)
        result = await service.cover(prompt="acoustic cover", audio_file=str(audio_file))

    assert result["success"] is True
    assert result["audio_base64"] == base64.b64encode(b"hello").decode()
    # 上传请求存在
    assert len(_requests(m, "POST", UPLOAD_URL)) == 1
    # 翻唱请求 payload 含 file_id 与 prompt
    cover_calls = _requests(m, "POST", COVER_URL)
    assert len(cover_calls) == 1
    cover_payload = cover_calls[0].kwargs["json"]
    assert cover_payload["file_id"] == 12345
    assert cover_payload["prompt"] == "acoustic cover"
    assert cover_payload["output_format"] == "hex"


async def test_cover_missing_audio(service):
    """audio_file 和 audio_url 都没传 → 返回错误，不发起请求。"""
    with aioresponses() as m:
        result = await service.cover(prompt="acoustic cover")
    assert result["success"] is False
    assert "audio_file" in result["error"] or "audio_url" in result["error"]
    assert len(m.requests) == 0


# ----------------------------------------------------------------------
# 错误码分类
# ----------------------------------------------------------------------

def test_classify_error(service):
    """验证 1002→retryable，1004/1008/1026/2013→fatal，未知→fatal。"""
    assert service._classify_error(1002) == "retryable"
    for code in (1004, 1008, 1026, 2013):
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

    service.update_model("music-2.5+")
    assert service._model == "music-2.5+"
