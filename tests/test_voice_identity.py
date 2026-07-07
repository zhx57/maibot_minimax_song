"""VoiceIdentityManager 单元测试。

覆盖：缓存读写、人格变更失效、prompt_fragment 构造、regenerate、
内存缓存命中、close 清空内存。
"""
import json
from unittest.mock import patch

import pytest

from voice_identity import VoiceIdentityManager


@pytest.fixture
def mgr(tmp_cache_dir, mock_logger):
    return VoiceIdentityManager(cache_dir=tmp_cache_dir, logger=mock_logger)


# ----------------------------------------------------------------------
# 首次构建与缓存写入
# ----------------------------------------------------------------------

def test_first_build_writes_cache(mgr, tmp_cache_dir):
    """首次调用 get_or_build，验证缓存文件创建，JSON 含必要字段。"""
    pf = mgr.get_or_build("麦麦", "温柔的人格描述", "zh")
    assert pf  # 非空

    cache_files = list(tmp_cache_dir.glob("*.json"))
    assert len(cache_files) == 1
    data = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert data["name"] == "麦麦"
    assert data["personality"] == "温柔的人格描述"
    assert data["prompt_fragment"] == pf
    assert "cached_at" in data


def test_cache_hit_reuses_fragment(mgr):
    """第二次调用相同 nickname+personality，返回相同 prompt_fragment，不重新构建。"""
    pf1 = mgr.get_or_build("麦麦", "活泼", "zh")
    # 第二次：mock _interpret_personality 验证不被调用
    with patch.object(mgr, "_interpret_personality") as mock_interp:
        pf2 = mgr.get_or_build("麦麦", "活泼", "zh")
        mock_interp.assert_not_called()
    assert pf1 == pf2


def test_personality_change_invalidates_cache(mgr):
    """先缓存 personality A，再传 personality B，验证重新构建。"""
    pf1 = mgr.get_or_build("麦麦", "活泼", "zh")
    with patch.object(
        mgr, "_interpret_personality", wraps=mgr._interpret_personality
    ) as mock_interp:
        pf2 = mgr.get_or_build("麦麦", "神秘", "zh")
        mock_interp.assert_called_once()
    # 两种人格应产生不同 prompt_fragment
    assert pf1 != pf2


def test_regenerate_deletes_cache(mgr, tmp_cache_dir):
    """regenerate 后缓存文件删除。"""
    mgr.get_or_build("麦麦", "活泼", "zh")
    assert len(list(tmp_cache_dir.glob("*.json"))) == 1
    mgr.regenerate("麦麦")
    assert len(list(tmp_cache_dir.glob("*.json"))) == 0


# ----------------------------------------------------------------------
# prompt_fragment 内容
# ----------------------------------------------------------------------

def test_prompt_fragment_contains_language(mgr):
    """language='zh' 时 prompt_fragment 含 'Mandarin Chinese'。"""
    pf = mgr.get_or_build("麦麦", "活泼", "zh")
    assert "Mandarin Chinese" in pf


def test_prompt_fragment_english_output(mgr):
    """prompt_fragment 是英文（含 'Vocal:' 和 'singing in'）。"""
    pf = mgr.get_or_build("麦麦", "活泼", "zh")
    assert "Vocal:" in pf
    assert "singing in" in pf


def test_empty_nickname_returns_empty(mgr):
    """nickname='' 返回空字符串。"""
    pf = mgr.get_or_build("", "any personality", "zh")
    assert pf == ""


# ----------------------------------------------------------------------
# 人格关键词映射
# ----------------------------------------------------------------------

def test_personality_keywords_mapping(mgr):
    """分别传含'活泼'/'神秘'/'温柔'的 personality，验证 timbre/style 不同。"""
    pf_huo = mgr.get_or_build("bot", "活泼", "zh")
    pf_shen = mgr.get_or_build("bot", "神秘", "zh")
    pf_wen = mgr.get_or_build("bot", "温柔", "zh")
    # 三者 timbre 不同 → prompt_fragment 不同
    assert pf_huo != pf_shen
    assert pf_shen != pf_wen
    assert pf_huo != pf_wen
    # 关键词映射的具体 timbre
    assert "bright and lively" in pf_huo
    assert "breathy and dark" in pf_shen
    assert "soft and warm" in pf_wen


# ----------------------------------------------------------------------
# 内存缓存
# ----------------------------------------------------------------------

def test_memory_cache_hit(mgr):
    """同一实例第二次调用走内存缓存（_read_cache 不被调用）。"""
    mgr.get_or_build("麦麦", "活泼", "zh")
    with patch.object(mgr, "_read_cache") as mock_read:
        pf = mgr.get_or_build("麦麦", "活泼", "zh")
        mock_read.assert_not_called()
    assert pf


def test_close_clears_memory(mgr):
    """close 后内存缓存清空，再次调用回退到读盘。"""
    mgr.get_or_build("麦麦", "活泼", "zh")
    mgr.close()
    # close 后内存清空，再次调用应回退到 _read_cache（磁盘缓存仍在）
    with patch.object(mgr, "_read_cache", wraps=mgr._read_cache) as mock_read:
        mgr.get_or_build("麦麦", "活泼", "zh")
        mock_read.assert_called_once()
