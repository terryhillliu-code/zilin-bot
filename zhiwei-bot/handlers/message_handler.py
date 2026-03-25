import logging
from core.llm_client import llm_client
from handlers.rag_handler import query_rag
from handlers.dy_handler import extract_dy_url, handle_dy_video
from handlers.ks_handler import extract_ks_url, handle_ks_video

logger = logging.getLogger(__name__)

def handle_message(event, context):
    """
    智能研究员「知微」的消息处理 - 增强多模态（视频外链）与 RAG 能力
    """
    msg = event.message
    msg_type = msg.msg_type
    sender_id = event.sender.sender_id.open_id
    message_id = msg.message_id
    
    # 1. 物理视频文件处理
    if msg_type == "video":
        logger.info(f"🎥 收到物理视频消息: {message_id}")
        return "🎬 收到视频文件，知微正在组织后端 Worker 进行转码与语音识别，请稍后在视频库中查看。"

    text = msg.content.strip()
    if not text:
        return
        
    logger.info(f"📩 研究员收到消息 (Type: {msg_type}): {text[:50]}")
    
    # 2. 短视频外链识别 (Douyin/Kuaishou)
    dy_url = extract_dy_url(text)
    if dy_url:
        logger.info(f"🔗 识别到抖音外链: {dy_url}")
        return handle_dy_video(dy_url, message_id, sender_id)
        
    ks_url = extract_ks_url(text)
    if ks_url:
        logger.info(f"🔗 识别到快手外链: {ks_url}")
        return handle_ks_video(ks_url, message_id, sender_id)

    # 3. RAG 识别逻辑：当包含关键词时触发知识库检索
    if any(k in text for k in ["笔记", "查一下", "知识库", "搜一下", "关于"]):
        logger.info("🔍 触发 RAG 检索循环...")
        rag_context = query_rag(text)
        text = f"{rag_context}\n\n请根据以上提供的关联知识内容，结合您的专业知识，详细回答用户问题：{text}"
    
    # 4. 调用 LLM 进行最终应答
    try:
        reply = llm_client.chat(msg.chat_id, text)
        return reply
    except Exception as e:
        logger.error(f"❌ LLM 请求异常: {e}")
        return "⚠️ 我现在有点累了，请稍后再试（AI 接口异常）。"
