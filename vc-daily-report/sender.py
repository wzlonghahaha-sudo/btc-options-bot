"""
全球顶级风投日报 - Telegram 推送模块
将生成的日报推送到 Telegram
"""
import logging
import requests
import time

from config import TG_BOT_TOKEN, TG_CHAT_ID

logger = logging.getLogger(__name__)

TG_API_BASE = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
MAX_MESSAGE_LENGTH = 4096  # Telegram 消息最大长度


def _split_message(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    将长消息按段落分割，确保不超过 Telegram 限制
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    current_chunk = ""

    # 按段落分割
    paragraphs = text.split("\n\n")

    for para in paragraphs:
        if len(current_chunk) + len(para) + 2 > max_len:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""

            # 如果单个段落就超长，强制按行分割
            if len(para) > max_len:
                lines = para.split("\n")
                for line in lines:
                    if len(current_chunk) + len(line) + 1 > max_len:
                        if current_chunk:
                            chunks.append(current_chunk.strip())
                        current_chunk = line + "\n"
                    else:
                        current_chunk += line + "\n"
            else:
                current_chunk = para + "\n\n"
        else:
            current_chunk += para + "\n\n"

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


def send_telegram_message(
    text: str,
    chat_id: str = None,
    parse_mode: str = "Markdown",
) -> bool:
    """
    发送消息到 Telegram
    支持自动分段发送长消息
    """
    chat_id = chat_id or TG_CHAT_ID

    if not TG_BOT_TOKEN or not chat_id:
        logger.error("Telegram 配置缺失: TG_BOT_TOKEN 或 TG_CHAT_ID 未设置")
        return False

    chunks = _split_message(text)
    total = len(chunks)
    success = True

    for i, chunk in enumerate(chunks):
        if total > 1:
            chunk_header = f"[{i+1}/{total}]\n\n" if i > 0 else ""
            chunk = chunk_header + chunk

        try:
            resp = requests.post(
                f"{TG_API_BASE}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )

            if resp.status_code == 200:
                result = resp.json()
                if result.get("ok"):
                    logger.info(f"Telegram 消息发送成功 ({i+1}/{total})")
                else:
                    # 如果 MarkdownV2 解析失败，降级为纯文本
                    logger.warning(f"Markdown 解析失败，降级为纯文本: {result.get('description')}")
                    resp2 = requests.post(
                        f"{TG_API_BASE}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                            "disable_web_page_preview": True,
                        },
                        timeout=30,
                    )
                    if resp2.status_code == 200 and resp2.json().get("ok"):
                        logger.info(f"纯文本消息发送成功 ({i+1}/{total})")
                    else:
                        logger.error(f"消息发送失败: {resp2.text}")
                        success = False
            else:
                # 降级到纯文本
                logger.warning(f"HTTP {resp.status_code}，降级为纯文本发送")
                resp2 = requests.post(
                    f"{TG_API_BASE}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    },
                    timeout=30,
                )
                if resp2.status_code == 200 and resp2.json().get("ok"):
                    logger.info(f"纯文本消息发送成功 ({i+1}/{total})")
                else:
                    logger.error(f"消息发送失败: {resp2.text}")
                    success = False

            # 多段消息间隔，避免 rate limit
            if i < total - 1:
                time.sleep(1)

        except Exception as e:
            logger.error(f"Telegram 发送异常: {e}")
            success = False

    return success


def send_report(report: str, chat_id: str = None) -> bool:
    """
    发送风投日报到 Telegram
    """
    logger.info("正在推送风投日报到 Telegram...")
    return send_telegram_message(report, chat_id=chat_id)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    # 测试发送
    test_msg = "🧪 *风投日报系统测试*\n\n这是一条测试消息，系统工作正常！"
    result = send_report(test_msg)
    print(f"发送结果: {'成功' if result else '失败'}")
