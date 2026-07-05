#!/usr/bin/env python3
"""
Cerebras Systems (CBRS) IPO Monitor
====================================
Monitors SEC EDGAR filings, NASDAQ listing status, and news sources
for Cerebras IPO progress. Sends alerts via Telegram.

Data sources:
  1. SEC EDGAR EFTS API — new filings (S-1/A, 424B4, EFFECT, etc.)
  2. SEC EDGAR RSS — company filing feed
  3. NASDAQ quote check — detects when CBRS starts trading
  4. CNBC / Reuters headline scan — news keywords

Run: python3 monitor.py [--test] [--force]
  --test   Send a test message to verify Telegram config
  --force  Send report even if nothing changed since last run
"""

import json
import os
import sys
import time
import logging
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

with open(CONFIG_PATH) as f:
    CFG = json.load(f)

TG_TOKEN = CFG["telegram"]["bot_token"]
# Support both single chat_id (legacy) and multiple chat_ids
if "chat_ids" in CFG["telegram"]:
    TG_CHATS = CFG["telegram"]["chat_ids"]
elif "chat_id" in CFG["telegram"]:
    TG_CHATS = [CFG["telegram"]["chat_id"]]
else:
    TG_CHATS = []
STATE_FILE = Path(CFG["state_file"])
LOG_FILE   = Path(CFG["log_file"])

HEADERS = {
    "User-Agent": "CerebrasIPOMonitor/1.0 (personal research; contact: user@example.com)",
    "Accept": "application/json, text/html, application/atom+xml",
}

# SEC asks for ≥0.1s between requests
SEC_DELAY = 0.5

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cbrs-monitor")

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "last_run": None,
        "known_filings": [],       # list of accession numbers
        "trading_detected": False,
        "last_digest": "",         # hash of last report to avoid duplicates
    }


def save_state(state: dict):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """Send message to all configured Telegram chats. Returns True if all succeed."""
    if TG_TOKEN.startswith("YOUR_") or not TG_CHATS:
        log.warning("Telegram not configured — printing to console only")
        print("\n" + "=" * 60)
        print("TELEGRAM MESSAGE (not sent — config missing):")
        print(message)
        print("=" * 60 + "\n")
        return False

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    all_ok = True
    for chat_id in TG_CHATS:
        if str(chat_id).startswith("YOUR_"):
            continue
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                log.info(f"Telegram message sent to {chat_id}")
            else:
                log.error(f"Telegram API error for {chat_id}: {resp.status_code} {resp.text}")
                all_ok = False
        except Exception as e:
            log.error(f"Telegram send to {chat_id} failed: {e}")
            all_ok = False
    return all_ok

# ---------------------------------------------------------------------------
# 1. SEC EDGAR — check for new filings
# ---------------------------------------------------------------------------
def check_edgar_filings(state: dict) -> list[dict]:
    """Query SEC EFTS full-text search for Cerebras filings."""
    new_filings = []

    # Method A: EFTS API (structured search)
    efts_url = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q": '"cerebras systems"',
        "forms": "S-1,S-1/A,424B4,EFFECT,RW,424B1,424B3,424B4",
        "dateRange": "custom",
        "startdt": "2026-04-01",
        "enddt": "2026-12-31",
    }

    try:
        resp = requests.get(efts_url, params=params, headers=HEADERS, timeout=15)
        time.sleep(SEC_DELAY)
    except Exception:
        pass  # fallback to method B

    # Method B: Company filings page (more reliable)
    browse_url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcompany&CIK=0002021728&type=&dateb="
        "&owner=include&count=10&search_text=&action=getcompany"
    )
    try:
        resp = requests.get(browse_url, headers=HEADERS, timeout=15)
        time.sleep(SEC_DELAY)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Find the filings table
            table = soup.find("table", class_="tableFile2")
            if table:
                rows = table.find_all("tr")[1:]  # skip header
                for row in rows:
                    cols = row.find_all("td")
                    if len(cols) >= 4:
                        filing_type = cols[0].get_text(strip=True)
                        desc = cols[2].get_text(strip=True) if len(cols) > 2 else ""
                        date = cols[3].get_text(strip=True) if len(cols) > 3 else ""
                        link_tag = cols[1].find("a")
                        link = f"https://www.sec.gov{link_tag['href']}" if link_tag else ""

                        # Extract accession number from link
                        acc_no = ""
                        if link_tag and "href" in link_tag.attrs:
                            parts = link_tag["href"].split("/")
                            for p in parts:
                                if "-" in p and len(p) > 15:
                                    acc_no = p
                                    break

                        if acc_no and acc_no not in state["known_filings"]:
                            new_filings.append({
                                "type": filing_type,
                                "date": date,
                                "description": desc,
                                "link": link,
                                "accession": acc_no,
                            })
                            state["known_filings"].append(acc_no)
    except Exception as e:
        log.error(f"EDGAR browse failed: {e}")

    # Method C: RSS feed
    rss_url = CFG["cerebras"]["edgar_rss"]
    try:
        import feedparser
        feed = feedparser.parse(rss_url)
        time.sleep(SEC_DELAY)
        for entry in feed.entries[:10]:
            acc_no = entry.get("id", entry.get("link", ""))
            title = entry.get("title", "")
            link = entry.get("link", "")
            updated = entry.get("updated", "")

            if acc_no and acc_no not in state["known_filings"]:
                new_filings.append({
                    "type": title,
                    "date": updated,
                    "description": title,
                    "link": link,
                    "accession": acc_no,
                })
                state["known_filings"].append(acc_no)
    except Exception as e:
        log.warning(f"RSS feed parse failed: {e}")

    return new_filings


def check_critical_filings(state: dict) -> list[dict]:
    """
    Specifically check for IPO-critical filing types:
    - 424B4: Final prospectus (means IPO is priced!)
    - EFFECT: Registration statement declared effective
    - These signal imminent or completed IPO pricing.
    """
    critical = []
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcompany&CIK=0002021728&type=424&dateb="
        "&owner=include&count=5&search_text=&action=getcompany"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        time.sleep(SEC_DELAY)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table", class_="tableFile2")
            if table:
                rows = table.find_all("tr")[1:]
                for row in rows:
                    cols = row.find_all("td")
                    if len(cols) >= 4:
                        filing_type = cols[0].get_text(strip=True)
                        date = cols[3].get_text(strip=True) if len(cols) > 3 else ""
                        link_tag = cols[1].find("a")
                        link = f"https://www.sec.gov{link_tag['href']}" if link_tag else ""
                        critical.append({
                            "type": filing_type,
                            "date": date,
                            "link": link,
                        })
    except Exception as e:
        log.warning(f"Critical filing check failed: {e}")

    return critical


# ---------------------------------------------------------------------------
# 2. NASDAQ — check if CBRS is trading
# ---------------------------------------------------------------------------
def check_nasdaq_trading() -> Optional[dict]:
    """
    Try to get a quote for CBRS on NASDAQ.
    If we get a valid price, IPO has started trading.
    """
    urls_to_try = [
        # Yahoo Finance API (free, no auth)
        f"https://query1.finance.yahoo.com/v8/finance/chart/CBRS?range=1d&interval=1m",
        # Backup: simple quote page check
    ]

    for url in urls_to_try:
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; IPOMonitor/1.0)",
            }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                chart = data.get("chart", {})
                result = chart.get("result")
                if result and len(result) > 0:
                    meta = result[0].get("meta", {})
                    price = meta.get("regularMarketPrice")
                    prev_close = meta.get("previousClose")
                    market_state = meta.get("marketState", "")
                    if price and price > 0:
                        return {
                            "price": price,
                            "prev_close": prev_close,
                            "market_state": market_state,
                            "currency": meta.get("currency", "USD"),
                            "exchange": meta.get("exchangeName", ""),
                        }
                error = chart.get("error")
                if error:
                    log.info(f"CBRS not yet trading: {error.get('description', 'no data')}")
        except Exception as e:
            log.debug(f"Quote check failed for {url}: {e}")

    return None


# ---------------------------------------------------------------------------
# 3. News monitoring — keyword scan
# ---------------------------------------------------------------------------
def check_news() -> list[dict]:
    """Scan news RSS feeds for Cerebras IPO-related headlines."""
    news_items = []
    keywords = ["cerebras", "cbrs"]
    ipo_keywords = ["ipo", "pricing", "priced", "debut", "listing", "public", "offering", "roadshow"]

    feeds = [
        # CNBC Tech
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
        # Reuters Business
        "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
    ]

    for feed_url in feeds:
        try:
            import feedparser
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:20]:
                title = entry.get("title", "").lower()
                summary = entry.get("summary", "").lower()
                combined = title + " " + summary

                if any(k in combined for k in keywords):
                    if any(k in combined for k in ipo_keywords):
                        news_items.append({
                            "title": entry.get("title", ""),
                            "link": entry.get("link", ""),
                            "published": entry.get("published", ""),
                            "source": feed_url.split("/")[2],
                        })
        except Exception as e:
            log.debug(f"News feed parse failed: {e}")

    # Also check Google News RSS for Cerebras
    google_news_url = (
        "https://news.google.com/rss/search?"
        "q=Cerebras+Systems+IPO+CBRS&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        import feedparser
        feed = feedparser.parse(google_news_url)
        for entry in feed.entries[:10]:
            title = entry.get("title", "")
            news_items.append({
                "title": title,
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "source": "Google News",
            })
    except Exception as e:
        log.debug(f"Google News parse failed: {e}")

    return news_items


# ---------------------------------------------------------------------------
# Compose report
# ---------------------------------------------------------------------------
def compose_report(
    new_filings: list,
    critical_filings: list,
    trading_info: Optional[dict],
    news: list,
    state: dict,
    force: bool = False,
) -> Optional[str]:
    """Build the Telegram message. Returns None if nothing to report."""

    now = datetime.now(timezone.utc)
    beijing = now + timedelta(hours=8)
    et = now - timedelta(hours=4)  # approximate EDT

    sections = []

    # Header
    sections.append(
        f"<b>CBRS IPO Monitor</b>\n"
        f"<i>{beijing.strftime('%Y-%m-%d %H:%M')} (北京时间)</i>\n"
        f"<i>{et.strftime('%Y-%m-%d %H:%M')} (美东时间)</i>"
    )

    # URGENT: Trading detected
    is_urgent = False
    if trading_info:
        is_urgent = True
        price = trading_info["price"]
        ipo_price = CFG["cerebras"]["ipo_price"]
        premium = ((price - ipo_price) / ipo_price) * 100

        sections.append(
            f"\n🚨🚨🚨 <b>CBRS 已开盘交易!</b> 🚨🚨🚨\n\n"
            f"  当前价格: <b>${price:.2f}</b>\n"
            f"  IPO发行价: ${ipo_price:.2f}\n"
            f"  开盘溢价: <b>{premium:+.1f}%</b>\n"
            f"  交易所: {trading_info.get('exchange', 'N/A')}\n"
            f"  市场状态: {trading_info.get('market_state', 'N/A')}\n\n"
        )
        if premium < 25:
            sections.append("  📊 策略提示: 溢价<25%, 属于积极买入区间\n")
        elif premium < 50:
            sections.append("  📊 策略提示: 溢价25-50%, 谨慎买入, 等回调\n")
        else:
            sections.append("  📊 策略提示: 溢价>50%, 建议放弃追高, 等回调\n")
    else:
        sections.append("\n📊 <b>交易状态:</b> CBRS 尚未开盘交易")

    # New SEC filings
    if new_filings:
        is_urgent = True
        section = "\n📄 <b>SEC 新Filing:</b>\n"
        for f in new_filings:
            section += f"  • [{f['type']}] {f['date']}\n"
            if f.get("link"):
                section += f"    {f['link']}\n"
            # Flag critical filing types
            ftype = f["type"].upper()
            if "424B4" in ftype:
                section += "    ⚠️ <b>424B4 = 最终招股书 → IPO已定价!</b>\n"
            elif "EFFECT" in ftype:
                section += "    ⚠️ <b>EFFECT = 注册声明生效 → 即将开始交易!</b>\n"
            elif "S-1/A" in ftype:
                section += "    ℹ️ S-1/A修订 → 可能更新了价格区间\n"
        sections.append(section)
    else:
        sections.append("\n📄 <b>SEC Filing:</b> 无新Filing (最近: S-1/A 2026-05-04)")

    # Critical filing types check
    has_424b4 = any("424" in f.get("type", "").upper() for f in critical_filings)
    if has_424b4:
        sections.append(
            "\n🔔 <b>发现424B系列文件!</b> 这意味着IPO已最终定价，"
            "预计次日开盘交易。请立即做好买入准备!"
        )

    # News
    if news:
        section = "\n📰 <b>相关新闻:</b>\n"
        seen_titles = set()
        count = 0
        for n in news:
            title = n["title"]
            if title not in seen_titles and count < 5:
                section += f"  • {title}\n"
                if n.get("link"):
                    section += f"    {n['link']}\n"
                seen_titles.add(title)
                count += 1
        sections.append(section)

    # Timeline estimate
    if not trading_info:
        sections.append(
            "\n⏰ <b>预计时间线:</b>\n"
            "  S-1/A 提交: 2026-05-04 ✅\n"
            "  路演: 约 5月5日-5月14日\n"
            "  价格区间上调至 $150-$160 (据Reuters)\n"
            "  定价: 预计 5月14日-15日\n"
            "  开盘交易: 预计 5月15日-16日\n\n"
            "  关键信号:\n"
            "  • 424B4文件提交 = 已定价!\n"
            "  • EFFECT通知 = 注册声明生效!"
        )

    # Build full message
    full_message = "\n".join(sections)

    # Dedup: skip if identical to last report (unless forced or urgent)
    digest = hashlib.md5(full_message.encode()).hexdigest()
    if not force and not is_urgent and digest == state.get("last_digest", ""):
        log.info("No changes since last report, skipping")
        return None
    state["last_digest"] = digest

    return full_message


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    force = "--force" in sys.argv
    test_mode = "--test" in sys.argv

    log.info("=" * 50)
    log.info("CBRS IPO Monitor — starting check")

    if test_mode:
        msg = (
            "<b>CBRS IPO Monitor — 测试消息</b>\n\n"
            "Telegram 推送配置成功!\n"
            f"时间: {datetime.now(timezone.utc).isoformat()}\n\n"
            "监控内容:\n"
            "• SEC EDGAR 新Filing检测\n"
            "• NASDAQ CBRS 开盘交易检测\n"
            "• IPO相关新闻聚合\n"
            "• 开盘溢价 & 买入策略提示"
        )
        send_telegram(msg)
        return

    state = load_state()

    # Run all checks
    log.info("Checking SEC EDGAR filings...")
    new_filings = check_edgar_filings(state)
    log.info(f"  Found {len(new_filings)} new filing(s)")

    log.info("Checking critical filing types (424B)...")
    critical_filings = check_critical_filings(state)
    log.info(f"  Found {len(critical_filings)} 424-type filing(s)")

    log.info("Checking NASDAQ trading status...")
    trading_info = check_nasdaq_trading()
    if trading_info:
        log.info(f"  TRADING DETECTED! Price: ${trading_info['price']}")
        state["trading_detected"] = True
    else:
        log.info("  Not yet trading")

    log.info("Checking news feeds...")
    news = check_news()
    log.info(f"  Found {len(news)} news item(s)")

    # Compose and send
    report = compose_report(
        new_filings, critical_filings, trading_info, news, state, force=force
    )

    if report:
        log.info("Sending report...")
        send_telegram(report)
    else:
        log.info("Nothing new to report")

    save_state(state)
    log.info("Check complete")


if __name__ == "__main__":
    main()
