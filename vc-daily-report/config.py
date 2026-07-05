"""
全球顶级风投日报 - 配置文件
"""
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# ============================================================
# Telegram 配置
# ============================================================
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# ============================================================
# OpenAI 配置
# ============================================================
AI_API_KEY = os.getenv("AI_API_KEY")
AI_API_BASE = os.getenv("AI_API_BASE", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")  # 使用 .env 中配置的模型

# ============================================================
# RSS 信息源 - 风投/创投新闻
# ============================================================
RSS_FEEDS = {
    # --- 核心风投/融资新闻 ---
    "TechCrunch Venture": "https://techcrunch.com/category/venture/feed/",
    "TechCrunch Startups": "https://techcrunch.com/category/startups/feed/",
    "Crunchbase News": "https://news.crunchbase.com/feed/",
    "VentureBeat": "https://venturebeat.com/category/ai/feed/",
    "The Information": "https://www.theinformation.com/feed",
    "Axios Pro Rata": "https://api.axios.com/feed/pro-rata",
    "PitchBook News": "https://pitchbook.com/news/feed",
    "Fortune Term Sheet": "https://fortune.com/tag/term-sheet/feed/",
    "Sifted EU VC": "https://sifted.eu/feed",
    "TechInAsia": "https://www.techinasia.com/feed",

    # --- AI/科技趋势 ---
    "AI News (VentureBeat)": "https://venturebeat.com/category/ai/feed/",
    "Wired Business": "https://www.wired.com/feed/category/business/latest/rss",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/technology-lab",

    # --- 加密/Web3 风投 ---
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "The Block": "https://www.theblock.co/rss.xml",

    # --- 中国科技/风投 ---
    "36Kr (English)": "https://36kr.com/feed",
    "PingWest": "https://en.pingwest.com/feed",
}

# ============================================================
# 网页抓取源 - 需要 HTML 解析
# ============================================================
WEB_SOURCES = {
    "CB Insights": {
        "url": "https://www.cbinsights.com/research/",
        "selector": "article",
    },
    "a16z Blog": {
        "url": "https://a16z.com/blog/",
        "selector": "article",
    },
    "Sequoia Perspectives": {
        "url": "https://www.sequoiacap.com/build/",
        "selector": "article",
    },
    "Y Combinator Blog": {
        "url": "https://www.ycombinator.com/blog",
        "selector": "article",
    },
}

# ============================================================
# Hacker News API - 追踪 YC 生态和技术趋势
# ============================================================
HN_API = {
    "top_stories": "https://hacker-news.firebaseio.com/v0/topstories.json",
    "item": "https://hacker-news.firebaseio.com/v0/item/{}.json",
}

# ============================================================
# Product Hunt API (GraphQL)
# ============================================================
PH_API = {
    "url": "https://www.producthunt.com/",
}

# ============================================================
# 重点追踪的顶级风投机构
# ============================================================
TOP_VC_FIRMS = [
    # --- 美国顶级 ---
    "Andreessen Horowitz", "a16z",
    "Sequoia Capital", "Sequoia",
    "Benchmark",
    "Accel", "Accel Partners",
    "Lightspeed Venture Partners", "Lightspeed",
    "Founders Fund",
    "Khosla Ventures",
    "Greylock Partners", "Greylock",
    "NEA", "New Enterprise Associates",
    "Bessemer Venture Partners", "Bessemer",
    "Index Ventures",
    "Insight Partners",
    "General Catalyst",
    "Coatue Management", "Coatue",
    "Tiger Global",
    "SoftBank Vision Fund", "SoftBank",
    "GGV Capital", "GGV",
    "Kleiner Perkins",
    "Union Square Ventures", "USV",
    "Ribbit Capital",
    "Thrive Capital",

    # --- 欧洲 ---
    "Atomico",
    "Balderton Capital",
    "Northzone",
    "EQT Ventures",

    # --- 中国/亚洲 ---
    "五源资本", "5Y Capital",
    "高瓴", "Hillhouse Capital",
    "红杉中国", "Sequoia China", "HongShan",
    "IDG Capital",
    "启明创投", "Qiming Venture Partners",
    "经纬创投", "Matrix Partners China",
    "真格基金", "ZhenFund",
    "源码资本", "Source Code Capital",
    "GIC",
    "Temasek",
]

# ============================================================
# 重点关注的行业赛道
# ============================================================
FOCUS_SECTORS = [
    "Artificial Intelligence", "AI", "Machine Learning", "LLM", "GenAI",
    "Fintech", "DeFi", "Crypto", "Web3", "Blockchain",
    "SaaS", "Enterprise Software", "Developer Tools",
    "Biotech", "Healthcare", "MedTech",
    "Climate Tech", "Clean Energy", "Sustainability",
    "Robotics", "Autonomous", "Self-driving",
    "Space Tech", "Defense Tech",
    "Cybersecurity", "Security",
    "E-commerce", "Marketplace",
    "EdTech", "Education",
    "Gaming", "Metaverse",
    "Quantum Computing",
    "Semiconductor", "Chips",
]

# ============================================================
# 数据存储路径
# ============================================================
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
