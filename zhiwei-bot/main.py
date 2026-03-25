import os
import sys
import logging
import threading
import time
import lark_oapi as lark
from handlers.event_handler import register_handlers

# 配置日志
LOG_DIR = "/Users/liufang/logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=f"{LOG_DIR}/feishu_bot.log",
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# 凭证
APP_ID = "cli_a917bc583a78dbcc"
APP_SECRET = "n65Xj21PgP4DvtmUYE3kedKGZHOZLdb5"

def hb():
    """生存心跳"""
    while True:
        logging.info("[HB-RESEARCHER] Zhiwei-Bot is active")
        time.sleep(300)

def main():
    # 启动心跳
    threading.Thread(target=hb, daemon=True).start()
    
    # 初始化事件处理器 (适配 v1.x/v2.x)
    event_handler = lark.EventDispatcherHandler.builder("", "").build()
    # 注册消息回调
    register_handlers(event_handler)
    
    # WebSocket 客户端 (适配 v1.x/v2.x)
    ws_client = lark.ws.Client(
        APP_ID, 
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )
    
    logging.info("🚀 [PRODUCTION] Zhiwei-Bot (Researcher) starting...")
    ws_client.start()

if __name__ == "__main__":
    main()
