"""消息去重与限流状态（2026-07-30 从 ws_client.py 拆分）

- processed_messages: 消息去重 deque (maxlen=500, 自动淘汰旧消息)，
  去重判断逻辑在 ws_client.do_p2_im_message_receive_v1 中
- user_last_request / RATE_LIMIT_SECONDS: 用户级限流（2s），
  判断逻辑在 command_handler.check_rate_limit（经 init_command_handler 注入）
"""

from collections import defaultdict, deque

# 限流
user_last_request = defaultdict(float)
RATE_LIMIT_SECONDS = 2

# 消息去重 (deque 自动淘汰旧消息)
processed_messages = deque(maxlen=500)
