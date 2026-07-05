# alert_channels.py
"""
CRITICAL 告警多通道降级: TG → SMTP → log.error
仅 CRITICAL 级别走此通道, 其余维持现状.
"""
import os
import time
import logging
import smtplib
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

_TG_MAX_RETRIES = 3
_TG_RETRY_DELAY = 2  # seconds


def _send_smtp(text: str) -> bool:
    """尝试通过 SMTP 发送告警邮件. 返回 True 表示成功."""
    host = os.getenv("SMTP_HOST", "").strip()
    port = os.getenv("SMTP_PORT", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    recipient = os.getenv("ALERT_EMAIL", "").strip()

    if not all([host, port, user, password, recipient]):
        log.warning("SMTP 未完整配置, 跳过邮件降级")
        return False

    try:
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = "⚠️ CRITICAL Alert — BTC Options Bot"
        msg["From"] = user
        msg["To"] = recipient

        with smtplib.SMTP(host, int(port), timeout=10) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(user, [recipient], msg.as_string())
        log.info("CRITICAL 告警已通过 SMTP 发送至 %s", recipient)
        return True
    except Exception:
        log.exception("SMTP 发送失败")
        return False


def send_critical(text: str, tg_send_func=None):
    """
    多通道降级发送 CRITICAL 告警:
      1. TG (重试 3 次, 每次间隔 2s)
      2. SMTP 邮件
      3. log.error 兜底

    不抛出任何异常, 保证调用方安全.
    """
    # ---- 1. TG 通道 ----
    if tg_send_func is not None:
        for attempt in range(1, _TG_MAX_RETRIES + 1):
            try:
                tg_send_func(text)
                log.info("CRITICAL 告警已通过 TG 发送 (第 %d 次)", attempt)
                return  # 成功即返回
            except Exception:
                log.warning(
                    "TG 发送失败 (第 %d/%d 次)", attempt, _TG_MAX_RETRIES
                )
                if attempt < _TG_MAX_RETRIES:
                    time.sleep(_TG_RETRY_DELAY)

        log.error("TG 发送全部失败, 降级到 SMTP")

    # ---- 2. SMTP 通道 ----
    if _send_smtp(text):
        return

    # ---- 3. log.error 兜底 ----
    log.error("CRITICAL 告警 (所有通道失败, 仅记录日志): %s", text)
