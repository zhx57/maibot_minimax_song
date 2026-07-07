"""pytest 公共 fixture 与 maibot_sdk 测试桩。

由于 ``maibot_sdk`` 在测试环境中不可安装，conftest 在导入时向 ``sys.modules``
注入一个基于 pydantic 的最小桩，使 ``plugin.py`` 可被正常导入并实例化。
所有 HTTP 调用通过 ``aioresponses`` mock，不需要真实 API Key。
"""
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ----------------------------------------------------------------------
# 1. 让测试既能 import music_service / plugin（顶层模块），也兼容包内导入
# ----------------------------------------------------------------------
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


# ----------------------------------------------------------------------
# 2. maibot_sdk 测试桩（必须在 plugin.py 被导入前注入）
# ----------------------------------------------------------------------
def _install_sdk_stub() -> None:
    if "maibot_sdk" in sys.modules:
        return
    import pydantic  # 仅测试环境需要

    sdk = types.ModuleType("maibot_sdk")
    sdk.CONFIG_RELOAD_SCOPE_SELF = "self"
    sdk.ON_BOT_CONFIG_RELOAD = "bot"

    def Field(default=None, default_factory=None, description=None, **kwargs):
        if default_factory is not None:
            return pydantic.Field(default_factory=default_factory, description=description)
        return pydantic.Field(default=default, description=description)

    class PluginConfigBase(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(arbitrary_types_allowed=True, extra="allow")

    class MaiBotPlugin:
        def __init__(self):
            self.ctx = None
            self.config = None

    def Tool(name, brief_description="", detailed_description="", parameters=None):
        def decorator(func):
            return func
        return decorator

    def API(name, description="", version="", public=False):
        def decorator(func):
            return func
        return decorator

    sdk.Field = Field
    sdk.PluginConfigBase = PluginConfigBase
    sdk.MaiBotPlugin = MaiBotPlugin
    sdk.Tool = Tool
    sdk.API = API

    types_module = types.ModuleType("maibot_sdk.types")

    class ToolParameterInfo:
        def __init__(self, name, param_type, description, required=False, default=None):
            self.name = name
            self.param_type = param_type
            self.description = description
            self.required = required
            self.default = default

    class ToolParamType:
        STRING = "string"
        BOOLEAN = "boolean"
        NUMBER = "number"
        INTEGER = "integer"

    types_module.ToolParameterInfo = ToolParameterInfo
    types_module.ToolParamType = ToolParamType
    sdk.types = types_module

    sys.modules["maibot_sdk"] = sdk
    sys.modules["maibot_sdk.types"] = types_module


_install_sdk_stub()

# ----------------------------------------------------------------------
# 3. pytest fixtures
# ----------------------------------------------------------------------
import pytest  # noqa: E402


@pytest.fixture
def tmp_cache_dir(tmp_path):
    """临时缓存目录。"""
    d = tmp_path / "voices"
    d.mkdir()
    return d


@pytest.fixture
def tmp_output_dir(tmp_path):
    """临时输出目录。"""
    d = tmp_path / "output"
    d.mkdir()
    return d


@pytest.fixture
def mock_logger():
    """mock logger。"""
    return MagicMock()


@pytest.fixture
def sample_hex_audio():
    """示例 hex 编码音频（"hello" 的 hex）。"""
    return "68656c6c6f"  # "hello"


@pytest.fixture
def sample_audio_bytes():
    return b"hello"


@pytest.fixture
def sample_api_response_success(sample_hex_audio):
    """MiniMax API 成功响应。"""
    return {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "data": {"status": 2, "audio": sample_hex_audio, "duration": 30000},
    }


@pytest.fixture
def mock_ctx(tmp_path):
    """mock MaiBot ctx 对象。"""
    ctx = MagicMock()
    ctx.paths.data_dir = tmp_path / "data"
    ctx.paths.data_dir.mkdir(parents=True, exist_ok=True)
    ctx.paths.runtime_dir = tmp_path / "runtime"
    ctx.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    ctx.logger = MagicMock()
    ctx.config = MagicMock()
    ctx.config.get = MagicMock(return_value={})
    ctx.config.get_all = MagicMock(return_value={})
    ctx.send = MagicMock()
    ctx.send.text = AsyncMock(return_value=True)
    ctx.send.custom = AsyncMock(return_value=True)
    ctx.send.image = AsyncMock(return_value=True)
    ctx.chat = MagicMock()
    ctx.chat.get_group_streams = AsyncMock(return_value=[])
    ctx.chat.get_private_streams = AsyncMock(return_value=[])
    ctx.api = MagicMock()
    return ctx


@pytest.fixture
def fast_sleep():
    """将 ``asyncio.sleep`` 替换为 no-op AsyncMock，加速重试类测试。"""
    with patch("asyncio.sleep", new=AsyncMock()):
        yield
