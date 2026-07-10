"""MiniMax 音乐生成、翻唱预处理和歌词生成服务。"""

import asyncio
import base64
import logging
import random
from typing import Any, Optional

import aiohttp


class MiniMaxMusicService:
    """封装 MiniMax 音乐 API，并复用 HTTP 会话及重试策略。"""

    DEFAULT_API_BASE_URL = "https://api.minimaxi.com"
    SUPPORTED_MODELS = {
        "music-2.6",
        "music-2.6-free",
        "music-cover",
        "music-cover-free",
    }
    GENERATION_MODELS = {"music-2.6", "music-2.6-free"}
    COVER_MODELS = {"music-cover", "music-cover-free"}
    INSTRUMENTAL_MODELS = GENERATION_MODELS
    LYRICS_OPTIMIZER_MODELS = GENERATION_MODELS
    MAX_PROMPT_LENGTH = {"generation": 1500, "cover": 300}
    MAX_LYRICS_LENGTH = {"generation": 1500, "cover": 1000}
    RETRYABLE_CODES = {1002}
    FATAL_CODES = {1004, 1008, 1026, 2013, 2049}
    OUTPUT_FORMATS = {"hex", "url"}

    def __init__(
        self,
        api_key: str,
        api_base_url: str = "",
        model: str = "music-2.6-free",
        max_retries: int = 3,
        retry_backoff_base: float = 1.5,
        logger: Optional[logging.Logger] = None,
        output_format: str = "hex",
    ) -> None:
        self._api_key = api_key.strip() if api_key else ""
        self._api_base_url = (
            api_base_url.strip() if api_base_url else self.DEFAULT_API_BASE_URL
        ).rstrip("/")
        self._model = model
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff_base = float(retry_backoff_base)
        self._output_format = output_format
        self._logger = logger or logging.getLogger(__name__)
        self._session: Optional[aiohttp.ClientSession] = None

    def update_api_key(self, api_key: str) -> None:
        self._api_key = api_key.strip() if api_key else ""

    def update_api_base_url(self, api_base_url: str) -> None:
        self._api_base_url = (
            api_base_url.strip() if api_base_url else self.DEFAULT_API_BASE_URL
        ).rstrip("/")

    def update_model(self, model: str) -> None:
        self._model = model

    def update_output_format(self, output_format: str) -> None:
        self._output_format = output_format

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _classify_error(self, status_code: int) -> str:
        return "retryable" if status_code in self.RETRYABLE_CODES else "fatal"

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _validation_error(self, model: str, operation: str) -> Optional[str]:
        if model not in self.SUPPORTED_MODELS:
            return f"不支持的音乐模型：{model}"
        expected = self.GENERATION_MODELS if operation == "generation" else self.COVER_MODELS
        if model not in expected:
            return f"{operation} 不支持模型 {model}"
        if self._output_format not in self.OUTPUT_FORMATS:
            return "output_format 仅支持 hex 或 url"
        return None

    async def _post_json(self, path: str, payload: dict[str, Any], operation: str) -> dict[str, Any]:
        """发送 JSON 请求；仅限流、网络异常和非 JSON 响应会重试。"""
        url = f"{self._api_base_url}{path}"
        last_error = "unknown error"
        last_code = -1
        for attempt in range(self._max_retries + 1):
            try:
                session = await self._get_session()
                async with session.post(url, json=payload, headers=self._headers()) as response:
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        data = None
                    if isinstance(data, dict):
                        base_resp = data.get("base_resp") or {}
                        code = base_resp.get("status_code", -1)
                        try:
                            last_code = int(code)
                        except (TypeError, ValueError):
                            last_code = -1
                        if last_code == 0:
                            return {"success": True, "response": data}
                        last_error = f"status_code={last_code}, msg={base_resp.get('status_msg', 'unknown error')}"
                        if self._classify_error(last_code) == "fatal":
                            return {"success": False, "error": last_error, "code": last_code}
                    else:
                        last_error = f"HTTP {response.status}: invalid response"
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"Network error: {exc}"
            except Exception as exc:
                self._logger.exception("%s 请求发生未预期异常", operation)
                return {"success": False, "error": str(exc), "code": last_code}

            if attempt < self._max_retries:
                delay = self._retry_backoff_base**attempt + random.uniform(0, 1)
                self._logger.warning(
                    "%s 请求失败（%d/%d）：%s，%.2f 秒后重试",
                    operation, attempt + 1, self._max_retries + 1, last_error, delay,
                )
                await asyncio.sleep(delay)
        return {"success": False, "error": f"重试耗尽：{last_error}", "code": last_code}

    async def _download_audio(self, url: str) -> bytes:
        session = await self._get_session()
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()

    async def _parse_music_response(
        self, response: dict[str, Any], fmt: str, operation: str
    ) -> dict[str, Any]:
        data = response.get("data") or {}
        if data.get("status") != 2:
            return {"success": False, "error": f"data.status={data.get('status')}，预期为 2", "code": 0}
        audio = data.get("audio")
        if not isinstance(audio, str) or not audio:
            return {"success": False, "error": "响应中没有音频数据", "code": 0}
        try:
            raw = bytes.fromhex(audio) if self._output_format == "hex" else await self._download_audio(audio)
        except (ValueError, TypeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._logger.warning("%s 音频解析失败：%s", operation, exc)
            return {"success": False, "error": f"音频解析失败：{exc}", "code": 0}

        extra = response.get("extra_info") or {}
        duration = extra.get("music_duration", data.get("duration", 0))
        return {
            "success": True,
            "audio_base64": base64.b64encode(raw).decode("ascii"),
            "format": fmt,
            "duration": duration,
            "size": len(raw),
        }

    async def generate(
        self,
        prompt: str,
        lyrics: Optional[str] = None,
        is_instrumental: bool = False,
        lyrics_optimizer: bool = False,
        sample_rate: int = 44100,
        bitrate: int = 256000,
        fmt: str = "mp3",
    ) -> dict[str, Any]:
        """使用配置的生成模型创作音乐。"""
        error = self._validation_error(self._model, "generation")
        if error:
            return {"success": False, "error": error}
        prompt = str(prompt or "")
        if len(prompt) > self.MAX_PROMPT_LENGTH["generation"]:
            return {"success": False, "error": "prompt 超过 1500 字符上限"}
        if lyrics is not None and not 1 <= len(lyrics) <= self.MAX_LYRICS_LENGTH["generation"]:
            return {"success": False, "error": "lyrics 长度需在 1-1500 字符之间"}
        if is_instrumental and self._model not in self.INSTRUMENTAL_MODELS:
            return {"success": False, "error": "is_instrumental 仅生成模型支持"}
        if lyrics_optimizer and self._model not in self.LYRICS_OPTIMIZER_MODELS:
            return {"success": False, "error": "lyrics_optimizer 仅生成模型支持"}

        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "is_instrumental": is_instrumental,
            "audio_setting": {"sample_rate": sample_rate, "bitrate": bitrate, "format": fmt},
            "output_format": self._output_format,
        }
        if lyrics is not None:
            payload["lyrics"] = lyrics
        if lyrics_optimizer:
            payload["lyrics_optimizer"] = True
        result = await self._post_json("/v1/music_generation", payload, "音乐生成")
        if not result.get("success"):
            return result
        return await self._parse_music_response(result["response"], fmt, "音乐生成")

    def _cover_model(self) -> str:
        if self._model in self.COVER_MODELS:
            return self._model
        return "music-cover-free" if self._model.endswith("-free") else "music-cover"

    async def cover(
        self,
        prompt: str,
        audio_url: Optional[str] = None,
        audio_base64: Optional[str] = None,
        cover_feature_id: Optional[str] = None,
        lyrics: Optional[str] = None,
        sample_rate: int = 44100,
        bitrate: int = 256000,
        fmt: str = "mp3",
    ) -> dict[str, Any]:
        """通过 URL、base64 或预处理特征 ID 生成翻唱。"""
        model = self._cover_model()
        error = self._validation_error(model, "cover")
        if error:
            return {"success": False, "error": error}
        sources = [audio_url, audio_base64, cover_feature_id]
        if sum(bool(value) for value in sources) != 1:
            return {"success": False, "error": "audio_url、audio_base64、cover_feature_id 必须且只能提供一个"}
        prompt = str(prompt or "")
        if not 10 <= len(prompt) <= self.MAX_PROMPT_LENGTH["cover"]:
            return {"success": False, "error": "翻唱 prompt 长度需在 10-300 字符之间"}
        if lyrics is not None and not 10 <= len(lyrics) <= self.MAX_LYRICS_LENGTH["cover"]:
            return {"success": False, "error": "翻唱 lyrics 长度需在 10-1000 字符之间"}
        if cover_feature_id and lyrics is None:
            return {"success": False, "error": "使用 cover_feature_id 时必须提供 lyrics"}

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "audio_setting": {"sample_rate": sample_rate, "bitrate": bitrate, "format": fmt},
            "output_format": self._output_format,
        }
        for name, value in (("audio_url", audio_url), ("audio_base64", audio_base64), ("cover_feature_id", cover_feature_id)):
            if value:
                payload[name] = value
        if lyrics is not None:
            payload["lyrics"] = lyrics
        result = await self._post_json("/v1/music_generation", payload, "翻唱生成")
        if not result.get("success"):
            return result
        return await self._parse_music_response(result["response"], fmt, "翻唱生成")

    async def cover_preprocess(
        self, audio_url: Optional[str] = None, audio_base64: Optional[str] = None
    ) -> dict[str, Any]:
        """预处理参考音频，返回 24 小时有效的特征 ID 和格式化歌词。"""
        if bool(audio_url) == bool(audio_base64):
            return {"success": False, "error": "audio_url 和 audio_base64 必须且只能提供一个"}
        payload: dict[str, Any] = {"model": "music-cover"}
        payload["audio_url" if audio_url else "audio_base64"] = audio_url or audio_base64
        result = await self._post_json("/v1/music_cover_preprocess", payload, "翻唱预处理")
        if not result.get("success"):
            return result
        response = result["response"]
        feature_id = response.get("cover_feature_id")
        formatted_lyrics = response.get("formatted_lyrics")
        if not feature_id or not isinstance(formatted_lyrics, str):
            return {"success": False, "error": "预处理响应缺少 cover_feature_id 或 formatted_lyrics"}
        return {
            "success": True,
            "cover_feature_id": feature_id,
            "formatted_lyrics": formatted_lyrics,
            "structure_result": response.get("structure_result"),
            "audio_duration": response.get("audio_duration"),
        }

    async def generate_lyrics(
        self,
        mode: str,
        prompt: Optional[str] = None,
        lyrics: Optional[str] = None,
        title: Optional[str] = None,
    ) -> dict[str, Any]:
        """创作完整歌词，或编辑已有歌词。"""
        if mode not in {"write_full_song", "edit"}:
            return {"success": False, "error": "mode 仅支持 write_full_song 或 edit"}
        if mode == "edit" and not lyrics:
            return {"success": False, "error": "edit 模式必须提供 lyrics"}
        if prompt is not None and len(prompt) > 2000:
            return {"success": False, "error": "prompt 超过 2000 字符上限"}
        if lyrics is not None and len(lyrics) > 3500:
            return {"success": False, "error": "lyrics 超过 3500 字符上限"}
        payload: dict[str, Any] = {"mode": mode}
        if prompt:
            payload["prompt"] = prompt
        if lyrics:
            payload["lyrics"] = lyrics
        if title:
            payload["title"] = title
        result = await self._post_json("/v1/lyrics_generation", payload, "歌词生成")
        if not result.get("success"):
            return result
        response = result["response"]
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        generated = data.get("lyrics")
        if not isinstance(generated, str) or not generated:
            return {"success": False, "error": "歌词生成响应缺少 lyrics"}
        return {
            "success": True,
            "lyrics": generated,
            "song_title": data.get("song_title"),
            "style_tags": data.get("style_tags"),
        }
