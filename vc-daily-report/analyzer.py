"""
全球顶级风投日报 - AI 分析模块
使用 GPT-4o 分析采集到的数据，生成结构化日报
"""
import json
import logging
from datetime import datetime, timezone
from openai import OpenAI

from config import AI_API_KEY, AI_API_BASE, AI_MODEL, DATA_DIR

logger = logging.getLogger(__name__)

client = OpenAI(api_key=AI_API_KEY, base_url=AI_API_BASE)

# ============================================================
# 系统提示词 - VC 分析专家
# ============================================================
SYSTEM_PROMPT = """你是一位全球顶级风险投资行业分析师，拥有20年的 VC/PE 行业经验。
你的任务是分析今日全球风投市场的最新动态，为投资人和创业者提供一份专业、精炼的每日投资简报。

你的分析应该：
1. 专业精准 - 使用投资行业专业术语，数据准确
2. 洞察深刻 - 不只是汇总新闻，而是提供行业趋势判断
3. 可操作 - 指出值得关注的投资方向和初创企业
4. 中英双语 - 标题和关键术语保留英文，分析用中文

输出格式要求 (Telegram Markdown)：
- 使用 Telegram MarkdownV2 格式
- 标题用粗体 *标题*
- 链接用 [文本](URL)
- 重点词用 `代码标记`
- 分隔用 ━━━━━━━━━━━━━━━━━━
- 不要使用 # 标题语法（Telegram不支持）
- 特殊字符需要转义: . ! ( ) - = | { } > + 等需要加 \\
"""

USER_PROMPT_TEMPLATE = """以下是今日采集到的全球风投/科技投资领域的 {total_count} 条新闻和信息。
采集时间: {collected_at}

请分析这些信息，生成一份风投日报。日报需要包含以下板块：

━━━━━━━━━━━━━━━━━━

📊 *全球风投日报* | {date}

━━━━━━━━━━━━━━━━━━

🔥 *一、今日重磅融资交易 (Top Deals)*
- 列出今日最重要的3-5笔融资交易
- 包含: 公司名 | 轮次 | 金额 | 领投方 | 所属赛道
- 每条附上简短点评

💰 *二、活跃投资机构 (Active VCs)*
- 今日最活跃的 VC 机构有哪些动作
- 特别关注: a16z, Sequoia, Tiger Global, SoftBank, 红杉中国 等

🏭 *三、热门赛道分析 (Hot Sectors)*
- 今日融资集中在哪些行业赛道
- 各赛道的投资趋势分析
- AI, Fintech, Biotech, Climate 等重点赛道动向

🌟 *四、值得关注的初创企业 (Startups to Watch)*
- 3-5家值得关注的初创企业
- 包含: 公司简介、商业模式、为什么值得关注

📈 *五、趋势洞察 (Insights & Trends)*
- 今日数据反映的宏观投资趋势
- 值得关注的行业变化信号
- 投资人/创业者的 Actionable Takeaways

🌏 *六、中国/亚洲市场 (China & Asia)*
- 亚洲市场特别动态（如果有）
- 中国科技/创投相关新闻

━━━━━━━━━━━━━━━━━━

注意：
1. 如果某个板块今日没有足够信息，可以基于近期趋势做简要分析
2. 所有金额使用美元，保留具体数字
3. 公司名保留英文原名
4. 每条信息尽量附上来源链接
5. 确保输出适合 Telegram 阅读，简洁但有深度
6. 使用 Telegram MarkdownV2 格式

━━━━━━━━━━━━━━━━━━

今日采集的原始数据如下:

{articles_json}
"""


def analyze_and_generate_report(collected_data: dict) -> str:
    """
    使用 GPT-4o 分析采集数据并生成日报
    """
    articles = collected_data.get("articles", [])
    stats = collected_data.get("stats", {})

    if not articles:
        return "⚠️ 今日未采集到有效数据，日报暂停一天。"

    # 为了不超过 token 限制，对文章进行精简
    simplified_articles = []
    for art in articles[:100]:  # 最多传100条
        simplified_articles.append({
            "source": art.get("source", ""),
            "title": art.get("title", ""),
            "summary": art.get("summary", "")[:200],
            "url": art.get("url", ""),
            "published": art.get("published", ""),
        })

    articles_json = json.dumps(simplified_articles, ensure_ascii=False, indent=1)

    today = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    user_prompt = USER_PROMPT_TEMPLATE.format(
        total_count=len(articles),
        collected_at=collected_data.get("collected_at", ""),
        date=today,
        articles_json=articles_json,
    )

    logger.info(f"正在使用 {AI_MODEL} 生成日报分析...")
    logger.info(f"输入: {len(articles)} 条文章, ~{len(user_prompt)} 字符")

    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            max_tokens=4000,
        )

        report = response.choices[0].message.content
        token_usage = response.usage

        logger.info(f"日报生成完成! Tokens: "
                     f"prompt={token_usage.prompt_tokens}, "
                     f"completion={token_usage.completion_tokens}, "
                     f"total={token_usage.total_tokens}")

        # 添加尾部信息
        footer = (
            f"\n\n━━━━━━━━━━━━━━━━━━\n"
            f"📋 *数据来源统计*\n"
            f"• RSS 新闻源: {stats.get('rss', 0)} 条\n"
            f"• 网页抓取: {stats.get('web', 0)} 条\n"
            f"• Hacker News: {stats.get('hacker_news', 0)} 条\n"
            f"• Product Hunt: {stats.get('product_hunt', 0)} 条\n"
            f"• 共计: {stats.get('total', 0)} 条原始信息\n"
            f"• AI 模型: `{AI_MODEL}`\n"
            f"━━━━━━━━━━━━━━━━━━"
        )

        full_report = report + footer

        # 保存日报
        today_file = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report_path = f"{DATA_DIR}/report_{today_file}.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(full_report)
        logger.info(f"日报已保存: {report_path}")

        return full_report

    except Exception as e:
        error_msg = f"⚠️ AI 分析失败: {str(e)}"
        logger.error(error_msg)
        return error_msg


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    # 读取今日采集的原始数据
    import os
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = f"{DATA_DIR}/raw_{today}.json"

    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        report = analyze_and_generate_report(data)
        print(report)
    else:
        print(f"未找到今日数据文件: {filepath}")
        print("请先运行 collector.py 采集数据")
