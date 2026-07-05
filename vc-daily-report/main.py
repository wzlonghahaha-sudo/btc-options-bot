#!/usr/bin/env python3
"""
全球顶级风投日报 - 主程序
采集 -> 分析 -> 推送 完整流程
"""
import sys
import json
import logging
import argparse
from datetime import datetime, timezone

from collector import collect_all
from analyzer import analyze_and_generate_report
from sender import send_report
from config import DATA_DIR

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{DATA_DIR}/vc_daily.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("vc-daily-report")


def run_pipeline(skip_collect: bool = False, skip_send: bool = False):
    """
    执行完整的日报流水线:
    1. 采集数据
    2. AI 分析生成日报
    3. Telegram 推送
    """
    start_time = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("🚀 全球风投日报 Pipeline 启动")
    logger.info(f"   时间: {start_time.isoformat()}")
    logger.info("=" * 60)

    # Step 1: 采集数据
    if skip_collect:
        logger.info("跳过采集步骤，读取已有数据...")
        today = start_time.strftime("%Y-%m-%d")
        filepath = f"{DATA_DIR}/raw_{today}.json"
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                collected_data = json.load(f)
            logger.info(f"已加载数据: {collected_data['stats']['total']} 条")
        except FileNotFoundError:
            logger.error(f"未找到数据文件: {filepath}")
            logger.info("将执行数据采集...")
            collected_data = collect_all()
    else:
        logger.info("Step 1/3: 开始数据采集...")
        collected_data = collect_all()

    # Step 2: AI 分析
    logger.info("Step 2/3: AI 分析生成日报...")
    report = analyze_and_generate_report(collected_data)

    if report.startswith("⚠️"):
        logger.warning(f"日报生成异常: {report}")
        if not skip_send:
            send_report(report)
        return

    logger.info(f"日报生成完成，长度: {len(report)} 字符")

    # Step 3: 推送
    if skip_send:
        logger.info("跳过推送步骤")
        print("\n" + "=" * 60)
        print("日报预览:")
        print("=" * 60)
        print(report)
    else:
        logger.info("Step 3/3: 推送到 Telegram...")
        success = send_report(report)
        if success:
            logger.info("✅ 日报推送成功!")
        else:
            logger.error("❌ 日报推送失败!")

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(f"Pipeline 完成，总耗时: {elapsed:.1f}s")


def main():
    parser = argparse.ArgumentParser(description="全球风投日报系统")
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="仅采集数据，不分析和推送"
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="仅分析已有数据（跳过采集），不推送"
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="不推送到 Telegram（本地预览）"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="发送测试消息验证 Telegram 连通性"
    )

    args = parser.parse_args()

    if args.test:
        from sender import send_telegram_message
        msg = "🧪 *风投日报系统连通性测试*\n\n系统工作正常！每天北京时间 20:00 将推送全球风投日报。"
        result = send_telegram_message(msg)
        print(f"测试结果: {'成功 ✅' if result else '失败 ❌'}")
        return

    if args.collect_only:
        logger.info("仅执行数据采集...")
        result = collect_all()
        print(f"\n采集完成: {result['stats']['total']} 条数据")
        return

    if args.analyze_only:
        run_pipeline(skip_collect=True, skip_send=True)
        return

    run_pipeline(skip_send=args.no_send)


if __name__ == "__main__":
    main()
