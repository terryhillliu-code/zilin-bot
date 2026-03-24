"""
全局密钥加载器
从 ~/.secrets/global.env 加载环境变量
"""
import os
from pathlib import Path

def load_secrets(silent: bool = False):
    """从全局配置文件加载环境变量"""
    secrets_file = Path.home() / ".secrets" / "global.env"

    if not secrets_file.exists():
        if not silent:
            print(f"⚠️ 密钥文件不存在: {secrets_file}")
        return False

    try:
        with open(secrets_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key] = value

        if not silent:
            print(f"✅ 已加载环境变量: {secrets_file}")
        return True
    except Exception as e:
        if not silent:
            print(f"❌ 加载密钥失败: {e}")
        return False


if __name__ == "__main__":
    load_secrets()
    # 验证
    print(f"FEISHU_APP_ID: {os.environ.get('FEISHU_APP_ID', '未设置')[:12]}...")