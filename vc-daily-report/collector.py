"""
全球顶级风投日报 - 数据采集模块
从多个来源采集风投/融资/初创企业信息
"""
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
from bs4 import BeautifulSoup

from config import (
    RSS_FEEDS, WEB_SOURCES, HN_API,
    TOP_VC_FIRMS, FOCUS_SECTORS, DATA_DIR
)

logger = logging.getLogger(__name__)

# 通用请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}

# 24 小时时间窗口
LOOKBACK_HOURS = 48  # 回看48小时确保不遗漏


def _entry_id(title: str, link: str) -> str:
    """生成文章唯一ID"""
    raw = f"{title}:{link}"
    return hashlib.md5(raw.encode()).hexdigest()


def _is_vc_relevant(text: str) -> bool:
    """判断文本是否与风投/融资相关"""
    text_lower = text.lower()
    # 融资关键词
    funding_keywords = [
        "raise", "raised", "funding", "round", "series a", "series b",
        "series c", "series d", "series e", "seed", "pre-seed",
        "venture", "capital", "invest", "valuation", "unicorn",
        "ipo", "spac", "acquisition", "acquire", "merger",
        "billion", "million", "$", "fund", "portfolio",
        "startup", "start-up", "founded", "launch",
        "融资", "投资", "估值", "独角兽", "种子轮", "天使轮",
        "A轮", "B轮", "C轮", "上市", "收购",
    ]
    # 检查是否包含任何融资关键词
    if any(kw.lower() in text_lower for kw in funding_keywords):
        return True
    # 检查是否提到了顶级 VC
    if any(vc.lower() in text_lower for vc in TOP_VC_FIRMS):
        return True
    # 检查是否涉及重点行业
    if any(sector.lower() in text_lower for sector in FOCUS_SECTORS):
        return True
    return False


def _parse_date(entry) -> Optional[datetime]:
    """从 feedparser entry 解析日期"""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


# ============================================================
# 1. RSS Feed 采集
# ============================================================
def collect_rss_feeds() -> list[dict]:
    """从所有 RSS 源采集最新文章"""
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    def fetch_feed(name, url):
        result = []
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:30]:  # 每个源最多取30条
                title = getattr(entry, "title", "")
                link = getattr(entry, "link", "")
                summary = getattr(entry, "summary", "")
                pub_date = _parse_date(entry)

                # 过滤时间
                if pub_date and pub_date < cutoff:
                    continue

                # 合并文本用于判断相关性
                full_text = f"{title} {summary}"
                if not _is_vc_relevant(full_text):
                    continue

                result.append({
                    "id": _entry_id(title, link),
                    "source": name,
                    "source_type": "rss",
                    "title": title.strip(),
                    "url": link,
                    "summary": BeautifulSoup(summary, "html.parser").get_text()[:500] if summary else "",
                    "published": pub_date.isoformat() if pub_date else None,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })
            logger.info(f"[RSS] {name}: collected {len(result)} articles")
        except Exception as e:
            logger.warning(f"[RSS] {name} failed: {e}")
        return result

    # 并行采集所有 RSS 源
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_feed, name, url): name
            for name, url in RSS_FEEDS.items()
        }
        for future in as_completed(futures):
            articles.extend(future.result())

    return articles


# ============================================================
# 2. 网页抓取
# ============================================================
def collect_web_sources() -> list[dict]:
    """从网页源抓取文章"""
    articles = []

    def fetch_web(name, config):
        result = []
        try:
            resp = requests.get(config["url"], headers=HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            elements = soup.select(config["selector"])[:15]
            for elem in elements:
                # 提取标题和链接
                title_tag = elem.find(["h1", "h2", "h3", "h4", "a"])
                if not title_tag:
                    continue

                title = title_tag.get_text(strip=True)
                link = ""
                a_tag = title_tag if title_tag.name == "a" else title_tag.find("a")
                if a_tag and a_tag.get("href"):
                    link = a_tag["href"]
                    if link.startswith("/"):
                        from urllib.parse import urljoin
                        link = urljoin(config["url"], link)

                # 提取摘要
                summary_tag = elem.find("p")
                summary = summary_tag.get_text(strip=True)[:300] if summary_tag else ""

                full_text = f"{title} {summary}"
                if not title or len(title) < 10:
                    continue

                result.append({
                    "id": _entry_id(title, link),
                    "source": name,
                    "source_type": "web",
                    "title": title,
                    "url": link,
                    "summary": summary,
                    "published": None,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })
            logger.info(f"[WEB] {name}: collected {len(result)} articles")
        except Exception as e:
            logger.warning(f"[WEB] {name} failed: {e}")
        return result

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_web, name, config): name
            for name, config in WEB_SOURCES.items()
        }
        for future in as_completed(futures):
            articles.extend(future.result())

    return articles


# ============================================================
# 3. Hacker News - YC 生态 + 科技趋势
# ============================================================
def collect_hacker_news(limit: int = 50) -> list[dict]:
    """从 Hacker News 采集与风投相关的热帖"""
    articles = []
    try:
        resp = requests.get(HN_API["top_stories"], timeout=10)
        story_ids = resp.json()[:limit]

        def fetch_story(sid):
            try:
                r = requests.get(HN_API["item"].format(sid), timeout=5)
                item = r.json()
                if not item or item.get("type") != "story":
                    return None
                title = item.get("title", "")
                url = item.get("url", f"https://news.ycombinator.com/item?id={sid}")
                score = item.get("score", 0)

                # 只要高分帖子或与风投相关的
                if score < 50 and not _is_vc_relevant(title):
                    return None

                return {
                    "id": f"hn_{sid}",
                    "source": "Hacker News",
                    "source_type": "hn",
                    "title": title,
                    "url": url,
                    "summary": f"Score: {score} | Comments: {item.get('descendants', 0)}",
                    "published": datetime.fromtimestamp(
                        item.get("time", 0), tz=timezone.utc
                    ).isoformat() if item.get("time") else None,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                    "score": score,
                }
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=15) as executor:
            results = list(executor.map(fetch_story, story_ids))
            articles = [r for r in results if r is not None]

        logger.info(f"[HN] Collected {len(articles)} stories")
    except Exception as e:
        logger.warning(f"[HN] Failed: {e}")

    return articles


# ============================================================
# 4. Product Hunt - 新产品/初创企业
# ============================================================
def collect_product_hunt() -> list[dict]:
    """从 Product Hunt 首页抓取今日热门产品"""
    articles = []
    try:
        resp = requests.get(
            "https://www.producthunt.com/",
            headers=HEADERS,
            timeout=15
        )
        soup = BeautifulSoup(resp.text, "html.parser")

        # 提取产品信息
        for item in soup.select('[data-test="post-item"], [class*="post-item"]')[:15]:
            title_tag = item.find(["h3", "h2", "a"])
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            link = ""
            a_tag = item.find("a", href=True)
            if a_tag:
                href = a_tag["href"]
                link = f"https://www.producthunt.com{href}" if href.startswith("/") else href

            desc_tag = item.find("p")
            desc = desc_tag.get_text(strip=True)[:200] if desc_tag else ""

            if title and len(title) > 3:
                articles.append({
                    "id": _entry_id(title, link),
                    "source": "Product Hunt",
                    "source_type": "ph",
                    "title": title,
                    "url": link,
                    "summary": desc,
                    "published": datetime.now(timezone.utc).isoformat(),
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                })

        logger.info(f"[PH] Collected {len(articles)} products")
    except Exception as e:
        logger.warning(f"[PH] Failed: {e}")

    return articles


# ============================================================
# 主采集函数
# ============================================================
def collect_all() -> dict:
    """
    执行所有数据源采集，返回汇总结果
    """
    logger.info("=" * 60)
    logger.info("开始全球风投数据采集...")
    logger.info("=" * 60)

    start_time = time.time()
    all_articles = []

    # 并行执行四大采集任务
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_rss = executor.submit(collect_rss_feeds)
        future_web = executor.submit(collect_web_sources)
        future_hn = executor.submit(collect_hacker_news)
        future_ph = executor.submit(collect_product_hunt)

        rss_articles = future_rss.result()
        web_articles = future_web.result()
        hn_articles = future_hn.result()
        ph_articles = future_ph.result()

    all_articles.extend(rss_articles)
    all_articles.extend(web_articles)
    all_articles.extend(hn_articles)
    all_articles.extend(ph_articles)

    # 去重
    seen_ids = set()
    unique_articles = []
    for art in all_articles:
        if art["id"] not in seen_ids:
            seen_ids.add(art["id"])
            unique_articles.append(art)

    elapsed = time.time() - start_time

    result = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "total": len(unique_articles),
            "rss": len(rss_articles),
            "web": len(web_articles),
            "hacker_news": len(hn_articles),
            "product_hunt": len(ph_articles),
            "elapsed_seconds": round(elapsed, 1),
        },
        "articles": unique_articles,
    }

    # 保存原始数据
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = f"{DATA_DIR}/raw_{today}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"采集完成: 共 {result['stats']['total']} 条 "
                f"(RSS:{result['stats']['rss']} Web:{result['stats']['web']} "
                f"HN:{result['stats']['hacker_news']} PH:{result['stats']['product_hunt']}) "
                f"耗时 {elapsed:.1f}s")
    logger.info(f"原始数据已保存: {filepath}")

    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    result = collect_all()
    print(f"\n📊 采集汇总:")
    print(f"   总计: {result['stats']['total']} 条")
    print(f"   RSS:  {result['stats']['rss']} 条")
    print(f"   Web:  {result['stats']['web']} 条")
    print(f"   HN:   {result['stats']['hacker_news']} 条")
    print(f"   PH:   {result['stats']['product_hunt']} 条")
    print(f"   耗时: {result['stats']['elapsed_seconds']}s")
