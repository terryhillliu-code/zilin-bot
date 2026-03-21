# zhiwei-bot 测试

## 运行测试

```bash
cd ~/zhiwei-bot
source venv/bin/activate
python3 tests/test_command_handler.py
```

## 测试覆盖

### test_command_handler.py

保护 `command_handler` 模块的关键功能：

1. **check_rate_limit 代码结构** - 确保不使用未定义的全局变量
2. **无 context 时的行为** - 应该跳过限流而不是崩溃
3. **有限流逻辑** - 有 context 时应该正常工作
4. **CommandContext 变量** - 必须提供 rate limit 所需变量
5. **视频 URL 提取** - 抖音分享链接解析

## 何时运行

- 修改 `command_handler.py` 后
- 修改 `command_context.py` 后
- 修改 `media_handler.py` 后
- 发布前