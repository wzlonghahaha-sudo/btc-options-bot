"""
期权链快照落盘 — 为回测积累数据

Binance 不提供期权历史链数据, 自建积累:
  - 每次扫描循环把完整链快照追加写入 data/chain_snapshots/YYYY-MM-DD.jsonl.gz
  - .env 开关 SNAPSHOT_ENABLED=true
  - 保留天数 SNAPSHOT_RETENTION_DAYS=90
  - 写入失败不影响主流程 (log.warning)

磁盘占用估算:
  每次快照约 300 个合约 × ~200 bytes/合约 ≈ 60KB 压缩后 ~10KB
  每 3 分钟一次 → 每天 ~480 次 × 10KB ≈ 5MB/天
  90 天保留 ≈ 450MB
"""

import os
import gzip
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", "data/chain_snapshots")
SNAPSHOT_ENABLED = os.getenv("SNAPSHOT_ENABLED", "false").lower() == "true"
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "90"))


def save_chain_snapshot(data: dict) -> bool:
    """
    保存一次期权链快照到 JSONL.GZ 文件

    Args:
        data: fetch_market_data 返回的数据字典, 含:
            spot, timestamp, marks (dict), tickers (dict)

    Returns:
        True 写入成功, False 写入失败 (不抛异常)
    """
    if not SNAPSHOT_ENABLED:
        return False

    try:
        spot = data.get("spot", 0)
        ts = data.get("timestamp", time.time())
        marks = data.get("marks", {})
        tickers = data.get("tickers", {})

        # 构建快照记录
        snapshot = {
            "ts": ts,
            "spot": spot,
            "contracts": [],
        }

        for sym, m in marks.items():
            if not sym.startswith("BTC"):
                continue
            t = tickers.get(sym, {})
            snapshot["contracts"].append({
                "s": sym,                                    # symbol
                "bid": float(t.get("bidPrice", 0)),
                "ask": float(t.get("askPrice", 0)),
                "mark": float(m.get("markPrice", 0)),
                "iv": float(m.get("markIV", 0)),
                "d": float(m.get("delta", 0)),               # delta
                "g": float(m.get("gamma", 0)),               # gamma
                "t": float(m.get("theta", 0)),               # theta
                "v": float(m.get("vega", 0)),                # vega
                "oi": float(t.get("volume", 0)),             # 用 volume 近似
            })

        # 确保目录存在
        Path(SNAPSHOT_DIR).mkdir(parents=True, exist_ok=True)

        # 按天分文件
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        filepath = os.path.join(SNAPSHOT_DIR, f"{date_str}.jsonl.gz")

        # 追加写入 (gzip append)
        line = json.dumps(snapshot, separators=(",", ":")) + "\n"
        with gzip.open(filepath, "at", encoding="utf-8") as f:
            f.write(line)

        return True

    except Exception as e:
        log.warning(f"期权链快照写入失败 (不影响主流程): {e}")
        return False


def cleanup_old_snapshots():
    """清理超过保留天数的快照文件"""
    if not SNAPSHOT_ENABLED:
        return

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_RETENTION_DAYS)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        snapshot_dir = Path(SNAPSHOT_DIR)
        if not snapshot_dir.exists():
            return

        removed = 0
        for f in snapshot_dir.glob("*.jsonl.gz"):
            # 文件名格式: YYYY-MM-DD.jsonl.gz
            date_part = f.stem.replace(".jsonl", "")
            if date_part < cutoff_str:
                f.unlink()
                removed += 1
                log.info(f"清理过期快照: {f.name}")

        if removed > 0:
            log.info(f"清理了 {removed} 个过期快照文件")

    except Exception as e:
        log.warning(f"清理过期快照失败: {e}")
