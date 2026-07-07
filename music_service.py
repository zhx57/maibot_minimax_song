"""MiniMax 音乐生成服务模块。

封装 MiniMax 音乐生成 HTTP 调用：
- ``generate``: POST /v1/music_generation（人声/纯音乐生成）
- ``cover``: 上传参考音频到 /v1/files/upload + POST /v1/music_cover（翻唱生成）

借鉴 maibot_voice 的 ``MiniMaxSyncTTSService`` 生产级模式：
aiohttp session 复用、错误码分类、指数退避重试、配置热重载、资源卸载清理。
响应 ``data.audio`` 为 hex 编码字符串，解码为原始字节后 base64 编码返回。
"""

import asyncio
import base64
import logging
import random
from pathlib import Path
from typing import Any, Optional

import aiohttp


class MiniMaxMusicService:
    """MiniMax 音乐生成服务。

    通过 POST /v1/music_generation 完成音乐生成（人声/纯音乐），
    通过 POST /v1/music_cover 完成翻唱生成。响应 ``data.audio`` 为 hex 编码字符串，
    解码为原始字节后 base64 编码返回。

    错误码分类：
    - retryable（重试）：1002 限流 / 网络异常 / 超时
    - fatal（立即返回）：1004 鉴权失败 / 1008 余额不足 / 1026 内容违规 / 2013 参数错误 / 未知码
    """

    DEFAULT_API_BASE_URL = "https://api.minimaxi.com"
    # 支持的音乐生成模型
    SUPPORTED_MODELS = {"music-2.5+", "music-2.5", "music-2.6-free"}
    # 支持 instrumental（纯音乐）模式的模型
    INSTRUMENTAL_MODELS = {"music-2.5+", "music-2.6-free"}
    # 翻唱端点默认模型
    COVER_MODEL = "music-cover-free"
    # 文本长度上限
    MAX_PROMPT_LENGTH = 2000
    MAX_LYRICS_LENGTH = 3500
    MIN_LYRICS_LENGTH = 1
    # 错误码分类
    RETRYABLE_CODES = {1002}  # 限流
    FATAL_CODES = {1004, 1008, 1026, 2013}  # 鉴权/余额/违规/参数

    def __init__(
        self,
        api_key: str,
        api_base_url: str = "",
        model: str = "music-2.6-free",
        max_retries: int = 3,
        retry_backoff_base: float = 1.5,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._api_key: str = api_key.strip() if api_key else ""
        self._api_base_url: str = (
            api_base_url.strip() if api_base_url else self.DEFAULT_API_BASE_URL
        ).rstrip("/")
        self._model: str = model
        self._max_retries: int = int(max_retries)
        self._retry_backoff_base: float = float(retry_backoff_base)
        self._logger: logging.Logger = logger or logging.getLogger(__name__)
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # 配置热重载
    # ------------------------------------------------------------------

    def update_api_key(self, api_key: str) -> None:
        """热重载时更新 API Key（无需重建 session）。"""
        self._api_key = api_key.strip() if api_key else ""

    def update_api_base_url(self, api_base_url: str) -> None:
        """热重载时更新 API Base URL（无需重建 session）。"""
        self._api_base_url = (
            api_base_url.strip() if api_base_url else self.DEFAULT_API_BASE_URL
        ).rstrip("/")

    def update_model(self, model: str) -> None:
        """热重载时更新模型（无需重建 session）。"""
        self._model = model

    # ------------------------------------------------------------------
    # HTTP 基础设施
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建复用的 ClientSession（timeout=600 秒，音乐生成耗时较长）。"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=600)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _headers(self) -> dict[str, str]:
        """返回 JSON 请求所需的鉴权头。"""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _classify_error(self, status_code: int) -> str:
        """返回 ``'retryable'`` 或 ``'fatal'``。未知码归 fatal。"""
        if status_code in self.RETRYABLE_CODES:
            return "retryable"
        return "fatal"

    async def close(self) -> None:
        """关闭 ClientSession，释放资源。"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # 音乐生成
    # ------------------------------------------------------------------

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
        """调用 POST /v1/music_generation 生成音乐。

        成功返回 ``{"success": True, "audio_base64": <str>, "format": <str>,
        "duration": <ms>, "size": <bytes>}``；
        失败返回 ``{"success": False, "error": <msg>}`` 或
        ``{"success": False, "error": <msg>, "code": <status_code>}``。

        重试策略：retryable 错误（1002 限流 / 网络异常 / 超时）按
        ``retry_backoff_base ** attempt + jitter`` 退避后重试，最多 ``max_retries`` 次；
        fatal 错误（1004/1008/1026/2013/未知）立即返回。
        """
        # 1. 文本长度校验
        if len(prompt) > self.MAX_PROMPT_LENGTH:
            return {"success": False, "error": "prompt 超过 2000 字符上限"}
        if lyrics is not None:
            if len(lyrics) < self.MIN_LYRICS_LENGTH or len(lyrics) > self.MAX_LYRICS_LENGTH:
                return {
                    "success": False,
                    "error": f"lyrics 长度需在 {self.MIN_LYRICS_LENGTH}-{self.MAX_LYRICS_LENGTH} 字符之间",
                }
        if is_instrumental and self._model not in self.INSTRUMENTAL_MODELS:
            return {
                "success": False,
                "error": "instrumental 模式仅 music-2.5+ / music-2.6-free 支持",
            }

        # 2. 构造 payload
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "is_instrumental": is_instrumental,
            "audio_setting": {
                "sample_rate": sample_rate,
                "bitrate": bitrate,
                "format": fmt,
                "channel": 1,
            },
            "output_format": "hex",
        }
        if not is_instrumental:
            if lyrics:
                payload["lyrics"] = lyrics
            elif lyrics_optimizer:
                payload["lyrics_optimizer"] = True

        # 3. 重试循环
        url = f"{self._api_base_url}/v1/music_generation"
        last_error = "unknown error"
        last_code: int = -1

        for attempt in range(self._max_retries + 1):
            try:
                session = await self._get_session()
                async with session.post(
                    url, json=payload, headers=self._headers()
                ) as response:
                    data: Optional[dict] = None
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        data = None

                    if isinstance(data, dict):
                        base_resp = data.get("base_resp", {}) or {}
                        sc = base_resp.get("status_code", -1)
                        last_code = sc

                        if sc == 0:
                            data_obj = data.get("data") or {}
                            status = data_obj.get("status")
                            hex_str = data_obj.get("audio")

                            if status == 2:
                                # 生成完成
                                if not hex_str:
                                    last_error = "No audio data in response"
                                    if attempt < self._max_retries:
                                        backoff = (
                                            self._retry_backoff_base ** attempt
                                            + random.uniform(0, 1)
                                        )
                                        self._logger.warning(
                                            "generate no audio (attempt=%d/%d): %s, retry in %.2fs",
                                            attempt + 1, self._max_retries, last_error, backoff,
                                        )
                                        await asyncio.sleep(backoff)
                                        continue
                                    return {
                                        "success": False,
                                        "error": f"重试耗尽：{last_error}",
                                        "code": sc,
                                    }

                                try:
                                    raw_bytes = bytes.fromhex(hex_str)
                                except (ValueError, TypeError) as e:
                                    last_error = f"hex decode failed: {e}"
                                    if attempt < self._max_retries:
                                        backoff = (
                                            self._retry_backoff_base ** attempt
                                            + random.uniform(0, 1)
                                        )
                                        self._logger.warning(
                                            "generate hex decode failed (attempt=%d/%d): %s, retry in %.2fs",
                                            attempt + 1, self._max_retries, last_error, backoff,
                                        )
                                        await asyncio.sleep(backoff)
                                        continue
                                    return {
                                        "success": False,
                                        "error": f"重试耗尽：{last_error}",
                                        "code": sc,
                                    }

                                # 4. 成功解析：hex → bytes → base64
                                audio_base64 = base64.b64encode(raw_bytes).decode("ascii")
                                duration = data_obj.get("duration", 0)
                                self._logger.info(
                                    "generate success: b64_len=%d, format=%s, size=%d, duration=%s",
                                    len(audio_base64), fmt, len(raw_bytes), duration,
                                )
                                return {
                                    "success": True,
                                    "audio_base64": audio_base64,
                                    "format": fmt,
                                    "duration": duration,
                                    "size": len(raw_bytes),
                                }

                            # data.status in (0, 1) 处理中/排队，理论上同步返回，
                            # 遇到非 2 视为失败重试
                            last_error = f"data.status={status}, expected 2"
                            if attempt < self._max_retries:
                                backoff = (
                                    self._retry_backoff_base ** attempt
                                    + random.uniform(0, 1)
                                )
                                self._logger.warning(
                                    "generate incomplete (attempt=%d/%d): %s, retry in %.2fs",
                                    attempt + 1, self._max_retries, last_error, backoff,
                                )
                                await asyncio.sleep(backoff)
                                continue
                            return {
                                "success": False,
                                "error": f"重试耗尽：{last_error}",
                                "code": sc,
                            }

                        # 其他 status_code → 错误码分类
                        error_msg = base_resp.get("status_msg", "unknown error")
                        last_error = f"status_code={sc}, msg={error_msg}"
                        classification = self._classify_error(sc)
                        if classification == "retryable" and attempt < self._max_retries:
                            backoff = (
                                self._retry_backoff_base ** attempt
                                + random.uniform(0, 1)
                            )
                            self._logger.warning(
                                "generate retryable error (attempt=%d/%d): %s, retry in %.2fs",
                                attempt + 1, self._max_retries, last_error, backoff,
                            )
                            await asyncio.sleep(backoff)
                            continue
                        # fatal / 重试耗尽：立即返回
                        return {"success": False, "error": last_error, "code": sc}

                    # 非 JSON 响应（多为 HTTP 错误），按瞬时错误重试
                    last_error = f"HTTP {response.status}: invalid response"
                    if attempt < self._max_retries:
                        backoff = (
                            self._retry_backoff_base ** attempt
                            + random.uniform(0, 1)
                        )
                        self._logger.warning(
                            "generate invalid response (attempt=%d/%d): %s, retry in %.2fs",
                            attempt + 1, self._max_retries, last_error, backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    return {"success": False, "error": f"重试耗尽：{last_error}"}

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = f"Network error: {e}"
                self._logger.warning(
                    "generate network error (attempt=%d/%d): %s",
                    attempt + 1, self._max_retries, e,
                )
                if attempt < self._max_retries:
                    backoff = (
                        self._retry_backoff_base ** attempt
                        + random.uniform(0, 1)
                    )
                    await asyncio.sleep(backoff)
                    continue
                return {
                    "success": False,
                    "error": f"重试耗尽：{last_error}",
                    "code": last_code,
                }
            except Exception as e:
                last_error = str(e)
                self._logger.error(
                    "generate error (attempt=%d/%d): %s",
                    attempt + 1, self._max_retries, e,
                )
                if attempt < self._max_retries:
                    backoff = (
                        self._retry_backoff_base ** attempt
                        + random.uniform(0, 1)
                    )
                    await asyncio.sleep(backoff)
                    continue
                return {
                    "success": False,
                    "error": f"重试耗尽：{last_error}",
                    "code": last_code,
                }

        return {"success": False, "error": f"重试耗尽：{last_error}", "code": last_code}

    # ------------------------------------------------------------------
    # 翻唱生成
    # ------------------------------------------------------------------

    async def cover(
        self,
        prompt: str,
        audio_file: Optional[str] = None,
        audio_url: Optional[str] = None,
        lyrics: Optional[str] = None,
    ) -> dict[str, Any]:
        """调用翻唱端点：上传参考音频 + POST /v1/music_cover。

        流程：
        1. 校验：audio_file 和 audio_url 至少一个，prompt 必填
        2. 若 audio_file：multipart 上传到 /v1/files/upload（purpose=music_cover）获取 file_id
           若 audio_url：直接在翻唱 payload 中传 url
        3. 调用 POST /v1/music_cover，payload 含 file_id/audio_url、prompt、model、output_format=hex
        4. 成功解析同 generate（hex 解码 + base64）
        5. 若翻唱端点不可用或返回未预期结构 → 返回 ``{"success": False,
           "error": "翻唱端点暂不可用：<详情>"}``
        """
        # 1. 参数校验
        if not prompt:
            return {"success": False, "error": "prompt is required"}
        if not audio_file and not audio_url:
            return {"success": False, "error": "audio_file 和 audio_url 至少提供一个"}

        # 文本长度校验
        if len(prompt) > self.MAX_PROMPT_LENGTH:
            return {"success": False, "error": "prompt 超过 2000 字符上限"}
        if lyrics is not None:
            if len(lyrics) < self.MIN_LYRICS_LENGTH or len(lyrics) > self.MAX_LYRICS_LENGTH:
                return {
                    "success": False,
                    "error": f"lyrics 长度需在 {self.MIN_LYRICS_LENGTH}-{self.MAX_LYRICS_LENGTH} 字符之间",
                }

        # 2. 获取 file_id（若提供 audio_file 则上传，若提供 audio_url 则跳过）
        file_id: Optional[int] = None
        if audio_file:
            upload_result = await self._upload_cover_audio(audio_file)
            if not upload_result.get("success"):
                return {
                    "success": False,
                    "error": (
                        "翻唱端点暂不可用：上传参考音频失败 - "
                        f"{upload_result.get('error', 'unknown')}"
                    ),
                }
            file_id = upload_result.get("file_id")

        # 3. 构造翻唱 payload
        cover_payload: dict[str, Any] = {
            "model": self.COVER_MODEL,
            "prompt": prompt,
            "output_format": "hex",
        }
        if file_id is not None:
            cover_payload["file_id"] = file_id
        if audio_url:
            cover_payload["audio_url"] = audio_url
        if lyrics:
            cover_payload["lyrics"] = lyrics

        # 4. 调用翻唱端点（带重试）
        url = f"{self._api_base_url}/v1/music_cover"
        last_error = "unknown error"
        last_code: int = -1

        for attempt in range(self._max_retries + 1):
            try:
                session = await self._get_session()
                async with session.post(
                    url, json=cover_payload, headers=self._headers()
                ) as response:
                    data: Optional[dict] = None
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        data = None

                    if isinstance(data, dict):
                        base_resp = data.get("base_resp", {}) or {}
                        sc = base_resp.get("status_code", -1)
                        last_code = sc

                        if sc == 0:
                            data_obj = data.get("data") or {}
                            status = data_obj.get("status")
                            hex_str = data_obj.get("audio")

                            if status == 2 and hex_str:
                                try:
                                    raw_bytes = bytes.fromhex(hex_str)
                                except (ValueError, TypeError) as e:
                                    last_error = f"hex decode failed: {e}"
                                    if attempt < self._max_retries:
                                        backoff = (
                                            self._retry_backoff_base ** attempt
                                            + random.uniform(0, 1)
                                        )
                                        await asyncio.sleep(backoff)
                                        continue
                                    return {
                                        "success": False,
                                        "error": f"翻唱端点暂不可用：{last_error}",
                                        "code": sc,
                                    }

                                audio_base64 = base64.b64encode(raw_bytes).decode("ascii")
                                duration = data_obj.get("duration", 0)
                                self._logger.info(
                                    "cover success: b64_len=%d, size=%d, duration=%s",
                                    len(audio_base64), len(raw_bytes), duration,
                                )
                                return {
                                    "success": True,
                                    "audio_base64": audio_base64,
                                    "format": "mp3",
                                    "duration": duration,
                                    "size": len(raw_bytes),
                                }

                            # status != 2 或无 audio → 翻唱端点未预期结构
                            last_error = (
                                f"data.status={status}, audio_present={bool(hex_str)}"
                            )
                            if attempt < self._max_retries:
                                backoff = (
                                    self._retry_backoff_base ** attempt
                                    + random.uniform(0, 1)
                                )
                                self._logger.warning(
                                    "cover incomplete (attempt=%d/%d): %s, retry in %.2fs",
                                    attempt + 1, self._max_retries, last_error, backoff,
                                )
                                await asyncio.sleep(backoff)
                                continue
                            return {
                                "success": False,
                                "error": f"翻唱端点暂不可用：{last_error}",
                                "code": sc,
                            }

                        # 其他 status_code → 错误码分类
                        error_msg = base_resp.get("status_msg", "unknown error")
                        last_error = f"status_code={sc}, msg={error_msg}"
                        classification = self._classify_error(sc)
                        if classification == "retryable" and attempt < self._max_retries:
                            backoff = (
                                self._retry_backoff_base ** attempt
                                + random.uniform(0, 1)
                            )
                            self._logger.warning(
                                "cover retryable error (attempt=%d/%d): %s, retry in %.2fs",
                                attempt + 1, self._max_retries, last_error, backoff,
                            )
                            await asyncio.sleep(backoff)
                            continue
                        return {
                            "success": False,
                            "error": f"翻唱端点暂不可用：{last_error}",
                            "code": sc,
                        }

                    # 非 JSON 响应
                    last_error = f"HTTP {response.status}: invalid response"
                    if attempt < self._max_retries:
                        backoff = (
                            self._retry_backoff_base ** attempt
                            + random.uniform(0, 1)
                        )
                        await asyncio.sleep(backoff)
                        continue
                    return {"success": False, "error": f"翻唱端点暂不可用：{last_error}"}

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = f"Network error: {e}"
                self._logger.warning(
                    "cover network error (attempt=%d/%d): %s",
                    attempt + 1, self._max_retries, e,
                )
                if attempt < self._max_retries:
                    backoff = (
                        self._retry_backoff_base ** attempt
                        + random.uniform(0, 1)
                    )
                    await asyncio.sleep(backoff)
                    continue
                return {
                    "success": False,
                    "error": f"翻唱端点暂不可用：{last_error}",
                    "code": last_code,
                }
            except Exception as e:
                last_error = str(e)
                self._logger.error(
                    "cover error (attempt=%d/%d): %s",
                    attempt + 1, self._max_retries, e,
                )
                if attempt < self._max_retries:
                    backoff = (
                        self._retry_backoff_base ** attempt
                        + random.uniform(0, 1)
                    )
                    await asyncio.sleep(backoff)
                    continue
                return {
                    "success": False,
                    "error": f"翻唱端点暂不可用：{last_error}",
                    "code": last_code,
                }

        return {
            "success": False,
            "error": f"翻唱端点暂不可用：{last_error}",
            "code": last_code,
        }

    # ------------------------------------------------------------------
    # 翻唱辅助：上传参考音频
    # ------------------------------------------------------------------

    async def _upload_cover_audio(self, audio_file: str) -> dict[str, Any]:
        """multipart 上传参考音频到 POST /v1/files/upload，purpose=music_cover。

        返回 ``{"success": True, "file_id": <int>}`` 或
        ``{"success": False, "error": <msg>}``。
        """
        if not audio_file:
            return {"success": False, "error": "audio_file is empty"}

        path = Path(audio_file)
        if not path.exists() or not path.is_file():
            return {"success": False, "error": f"Audio file not found: {audio_file}"}

        url = f"{self._api_base_url}/v1/files/upload"
        # multipart 上传只需 Authorization，boundary 由 aiohttp 自动设置
        auth_headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            session = await self._get_session()
            with open(path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("purpose", "music_cover")
                form.add_field(
                    "file",
                    f,
                    filename=path.name,
                    content_type="application/octet-stream",
                )
                async with session.post(url, data=form, headers=auth_headers) as response:
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        data = None

                    if not isinstance(data, dict):
                        return {
                            "success": False,
                            "error": f"invalid response: HTTP {response.status}",
                        }

                    base_resp = data.get("base_resp", {}) or {}
                    sc = base_resp.get("status_code", -1)
                    if sc != 0:
                        return {
                            "success": False,
                            "error": f"status_code={sc}, msg={base_resp.get('status_msg')}",
                        }

                    file_info = data.get("file", {}) or {}
                    file_id = file_info.get("file_id")
                    if file_id is None:
                        return {"success": False, "error": "no file_id in response"}

                    self._logger.info(
                        "Cover audio uploaded: file=%s, file_id=%s", path.name, file_id,
                    )
                    return {"success": True, "file_id": int(file_id)}
        except aiohttp.ClientError as e:
            self._logger.error("upload_cover_audio network error: %s", e)
            return {"success": False, "error": f"Network error: {e}"}
        except Exception as e:
            self._logger.error("upload_cover_audio error: %s", e)
            return {"success": False, "error": str(e)}
