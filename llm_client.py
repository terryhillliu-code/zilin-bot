"""
DEPRECATED: 此文件已废弃，请直接使用 from zhiwei_common.llm import ...
保留仅为向后兼容，将在下个版本删除。
"""
import warnings
warnings.warn("llm_client.py is deprecated, use zhiwei_common.llm directly", DeprecationWarning, stacklevel=2)
from zhiwei_common.llm import *  # noqa: F401,F403
from zhiwei_common.llm import llm_client  # noqa: F401  # 显式重导出全局实例，兼容旧代码
from zhiwei_common.llm import get_client  # noqa: F401  # 遗留 dict 风格客户端工厂，兼容旧代码
