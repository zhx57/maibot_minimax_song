"""buddy-sings 技能的声音身份管理模块。

将 MaiBot 自身人格作为"歌手"身份，基于 nickname + personality 创意解读出
声音特征（音色 / 风格 / 性别），构造英文 prompt_fragment 供 MiniMax 音乐
生成 API 使用，并在本地（内存 + 磁盘 JSON）缓存，保证同一 bot 跨会话
声音一致。当 bot 人格变更时自动失效重建。

本模块是纯逻辑实现，不依赖 MaiBot SDK，也不调用 MiniMax API。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any


class VoiceIdentityManager:
    """管理 buddy-sings 歌手声音身份的构建、缓存与失效。

    缓存两层：
    - 内存缓存 ``self._memory_cache``：避免每次都读盘
    - 磁盘缓存 ``<cache_dir>/<sanitized_nickname>.json``：跨进程/重启持久化

    缓存条目格式：
        {"name": <nickname>, "personality": <personality>,
         "prompt_fragment": <pf>, "cached_at": <unix ts>}

    当缓存中的 ``personality`` 与当前传入的不一致时，视为 bot 人格已变更，
    自动重建并覆盖缓存。
    """

    # 语言代码 → MiniMax prompt 中使用的英文语言名
    _LANGUAGE_MAP: dict[str, str] = {
        "zh": "Mandarin Chinese",
        "en": "English",
        "ja": "Japanese",
        "ko": "Korean",
    }

    # 人格关键词 → (timbre, style) 映射；按声明顺序匹配，命中即采用
    # 参考 buddy-sings SKILL.md 中的人格→声音解读逻辑
    _PERSONALITY_KEYWORDS: list[tuple[list[str], str, str]] = [
        (["简短", "few words", "话少", "沉默"], "low and measured", "sparse phrasing"),
        (["活泼", "energetic", "俏皮", "lively", "cheerful"], "bright and lively", "playful rhythmic delivery"),
        (["神秘", "mysterious", "冷", "cool"], "breathy and dark", "slow enigmatic phrasing"),
        (["慵懒", "chill", "懒", "relaxed"], "lazy and warm", "laid-back phrasing"),
        (["温柔", "gentle", "soft", "温暖"], "soft and warm", "tender phrasing"),
        (["严肃", "serious", "成熟", "mature"], "deep and steady", "measured phrasing"),
    ]

    _DEFAULT_TIMBRE = "warm and natural"
    _DEFAULT_STYLE = "natural phrasing"

    def __init__(self, cache_dir: Any, logger: logging.Logger | None = None) -> None:
        """初始化声音身份管理器。

        Args:
            cache_dir: 缓存目录，Path 对象或字符串均可。
            logger: 可选日志器；为 None 时使用 ``logging.getLogger(__name__)``。
        """
        self._cache_dir = Path(cache_dir)
        self._logger = logger or logging.getLogger(__name__)
        # 内存缓存：nickname -> cache entry dict，避免频繁读盘
        self._memory_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 缓存路径与读写
    # ------------------------------------------------------------------

    def _cache_path(self, nickname: str) -> Path:
        """返回 nickname 对应的缓存文件路径。

        sanitized：移除特殊字符，只保留字母数字、中文、下划线、横线，
        其余字符替换为下划线。
        """
        sanitized = re.sub(r"[^\w\u4e00-\u9fff-]", "_", nickname)
        return self._cache_dir / f"{sanitized}.json"

    def _read_cache(self, nickname: str) -> dict | None:
        """读取 nickname 的磁盘缓存。文件不存在或解析失败返回 None。"""
        path = self._cache_path(nickname)
        try:
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            self._logger.warning("Voice identity cache invalid format (not dict): %s", path)
            return None
        except Exception as e:
            self._logger.warning("Failed to read voice identity cache '%s': %s", path, e)
            return None

    def _write_cache(self, nickname: str, data: dict) -> None:
        """写 JSON 到 nickname 对应的缓存文件。异常时仅 log warning 不抛出。"""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._cache_path(nickname)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._logger.warning("Failed to write voice identity cache for '%s': %s", nickname, e)

    # ------------------------------------------------------------------
    # 核心：获取或构建 prompt_fragment
    # ------------------------------------------------------------------

    def get_or_build(self, nickname: str, personality: str, language: str = "zh") -> str:
        """获取或构建指定 nickname 的声音身份 prompt_fragment。

        流程：
        1. nickname 为空 → 返回空字符串并告警
        2. 命中内存缓存且 personality 一致 → 直接返回
        3. 命中磁盘缓存且 personality 一致 → 写入内存并返回
        4. 否则调用 ``_interpret_personality`` 重建，写入内存与磁盘缓存

        Args:
            nickname: 歌手昵称（bot.nickname）。
            personality: 人格描述（personality.personality）。
            language: 歌词语言代码（zh/en/ja/ko），默认 ``zh``。

        Returns:
            英文 prompt_fragment 字符串。
        """
        if not nickname:
            self._logger.warning("get_or_build called with empty nickname; returning empty prompt_fragment")
            return ""

        # 1. 内存缓存
        mem_entry = self._memory_cache.get(nickname)
        if mem_entry is not None and mem_entry.get("personality") == personality:
            pf = mem_entry.get("prompt_fragment")
            if isinstance(pf, str) and pf:
                return pf

        # 2. 磁盘缓存
        disk_entry = self._read_cache(nickname)
        if disk_entry is not None:
            if disk_entry.get("personality") == personality:
                # 命中且人格一致：写入内存缓存并复用
                pf = disk_entry.get("prompt_fragment")
                if isinstance(pf, str) and pf:
                    self._memory_cache[nickname] = disk_entry
                    return pf
                # prompt_fragment 异常，重建
            # personality 不一致 → 视为人格变更，重建

        # 3. 重建
        prompt_fragment = self._interpret_personality(nickname, personality, language)
        cache_data = {
            "name": nickname,
            "personality": personality,
            "prompt_fragment": prompt_fragment,
            "cached_at": time.time(),
        }
        self._memory_cache[nickname] = cache_data
        self._write_cache(nickname, cache_data)
        return prompt_fragment

    # ------------------------------------------------------------------
    # 人格→声音身份解读
    # ------------------------------------------------------------------

    def _interpret_personality(self, nickname: str, personality: str, language: str) -> str:
        """基于 nickname + personality 创意解读，构造英文 prompt_fragment。

        Args:
            nickname: 歌手昵称。
            personality: 人格描述文本。
            language: 语言代码（zh/en/ja/ko）。

        Returns:
            英文 prompt_fragment 字符串，形如::

                Vocal: <timbre> [<gender>] voice singing in <Language> with <style>.
                The singer is named <nickname>, characterized as: <personality_summary>.
        """
        language_name = self._LANGUAGE_MAP.get(language, "Mandarin Chinese")

        # 关键词映射：扫描 personality 文本，命中第一组关键词即采用
        text = personality or ""
        timbre = self._DEFAULT_TIMBRE
        style = self._DEFAULT_STYLE
        for keywords, t, s in self._PERSONALITY_KEYWORDS:
            for kw in keywords:
                if kw in text:
                    timbre = t
                    style = s
                    break
            if timbre != self._DEFAULT_TIMBRE or style != self._DEFAULT_STYLE:
                break

        # 性别推断
        gender = self._infer_gender(text)
        if gender == "female":
            voice_phrase = "female voice"
        elif gender == "male":
            voice_phrase = "male voice"
        else:
            # neutral：不指定性别，仅用 "voice"
            voice_phrase = "voice"

        # personality_summary：前 100 字符，移除换行
        personality_summary = re.sub(r"[\r\n]+", " ", text).strip()[:100]

        prompt_fragment = (
            f"Vocal: {timbre} {voice_phrase} singing in {language_name} with {style}.\n"
            f"The singer is named {nickname}, characterized as: {personality_summary}."
        )
        return prompt_fragment

    @staticmethod
    def _infer_gender(text: str) -> str:
        """从人格文本中推断性别。返回 ``female`` / ``male`` / ``neutral``。"""
        if any(kw in text for kw in ["女", "girl", "she", "女性"]):
            return "female"
        if any(kw in text for kw in ["男", "boy", "he", "男性"]):
            return "male"
        return "neutral"

    # ------------------------------------------------------------------
    # 维护方法
    # ------------------------------------------------------------------

    def regenerate(self, nickname: str) -> None:
        """删除 nickname 的磁盘缓存文件并清除内存缓存。

        下次调用 ``get_or_build`` 时会重新解读人格并写回缓存。
        """
        try:
            path = self._cache_path(nickname)
            if path.exists():
                path.unlink()
        except Exception as e:
            self._logger.warning("Failed to delete voice identity cache for '%s': %s", nickname, e)
        self._memory_cache.pop(nickname, None)
        self._logger.info("Voice identity cache regenerated for nickname: %s", nickname)

    def close(self) -> None:
        """清空内存缓存。磁盘缓存保留以便下次启动复用。"""
        self._memory_cache.clear()
        self._logger.info("Voice identity memory cache cleared")
