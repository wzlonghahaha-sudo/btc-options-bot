"""
币安期权(European Options) 数据获取工具
API文档: https://binance-docs.github.io/apidocs/voptions/en/

支持获取:
  公开接口 (无需 API Key):
    1. 交易对/合约列表 (exchangeInfo)
    2. 实时行情/Ticker
    3. K线数据
    4. 深度/订单簿
    5. 最近成交
    6. 标记价格
    7. 期权指数价格
    8. 历史成交
    9. Open Interest

  私有接口 (需要 API Key + Secret):
    10. 账户信息
    11. 当前持仓
    12. 下单/撤单
    13. 当前挂单
    14. 历史委托
    15. 成交历史
"""

import hashlib
import hmac
import logging
import os
import time
import requests
from datetime import datetime, timezone
from urllib.parse import urlencode

from dotenv import load_dotenv

# 从 .env 文件加载密钥
load_dotenv()

BASE_URL = "https://eapi.binance.com"

_log = logging.getLogger(__name__)

# 可重试的 HTTP 状态码
_RETRYABLE_STATUS = frozenset({429, 418, 500, 502, 503, 504})


class BinanceOptionsAPI:
    """币安期权 API 封装（支持公开接口 + 鉴权私有接口）"""

    # 重试配置
    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 1  # 秒

    def __init__(self, api_key: str = None, secret_key: str = None):
        self.api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self.secret_key = secret_key or os.getenv("BINANCE_SECRET_KEY", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
        })

        # serverTime 校准: 计算本地时钟与服务器时钟的偏移量 (ms)
        self._time_offset_ms = 0
        self._calibrate_server_time()

    def _calibrate_server_time(self) -> None:
        """启动时校准本地时钟与币安服务器时钟 (仅调用一次)"""
        try:
            local_before = int(time.time() * 1000)
            resp = self.session.get(f"{BASE_URL}/eapi/v1/time", timeout=5)
            resp.raise_for_status()
            server_time = resp.json().get("serverTime", 0)
            local_after = int(time.time() * 1000)
            # 取请求往返中点作为本地参考时间
            local_mid = (local_before + local_after) // 2
            self._time_offset_ms = server_time - local_mid
            _log.info(f"serverTime 校准成功, offset={self._time_offset_ms}ms")
        except Exception as e:
            _log.warning(f"serverTime 校准失败, 使用本地时间: {e}")
            self._time_offset_ms = 0

    def _sign(self, params: dict) -> dict:
        """为请求参数生成 HMAC SHA256 签名 (使用校准后的时间戳)"""
        params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        params["recvWindow"] = 10000
        query_string = urlencode(params)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, endpoint: str, params: dict = None,
                 signed: bool = False) -> dict | list:
        """
        统一 HTTP 请求方法, 带指数退避重试。

        重试策略:
          - 最多 MAX_RETRIES 次
          - 指数退避: delay = RETRY_BASE_DELAY * 2^attempt
          - 仅对网络错误 (ConnectionError, Timeout), HTTP 5xx, 429/418 重试
          - 429/418: 读取 Retry-After 头决定等待时长
          - 4xx (除 429/418) 和认证错误不重试
        """
        url = f"{BASE_URL}{endpoint}"
        headers = {}
        if signed:
            if not self.api_key or not self.secret_key:
                raise ValueError("需要 API Key 和 Secret Key 才能调用私有接口")
            headers["X-MBX-APIKEY"] = self.api_key

        last_exception = None
        for attempt in range(self.MAX_RETRIES):
            try:
                # 签名必须在每次重试时重新生成 (timestamp 会变)
                req_params = dict(params) if params else {}
                if signed:
                    req_params = self._sign(req_params)

                resp = self.session.request(
                    method, url, params=req_params, headers=headers, timeout=10
                )

                # 成功
                if resp.ok:
                    return resp.json()

                # 判断是否可重试
                status = resp.status_code
                if status in _RETRYABLE_STATUS:
                    last_exception = requests.exceptions.HTTPError(
                        f"{status} {resp.reason}", response=resp
                    )
                    # 429/418: 优先使用 Retry-After 头
                    if status in (429, 418):
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            wait = int(retry_after)
                        else:
                            wait = self.RETRY_BASE_DELAY * (2 ** attempt)
                        _log.warning(
                            f"HTTP {status} on {endpoint}, "
                            f"Retry-After={retry_after}, sleeping {wait}s "
                            f"(attempt {attempt + 1}/{self.MAX_RETRIES})"
                        )
                    else:
                        wait = self.RETRY_BASE_DELAY * (2 ** attempt)
                        _log.warning(
                            f"HTTP {status} on {endpoint}, "
                            f"sleeping {wait}s "
                            f"(attempt {attempt + 1}/{self.MAX_RETRIES})"
                        )
                    time.sleep(wait)
                    continue

                # 不可重试的 4xx — 直接抛出
                resp.raise_for_status()

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_exception = e
                wait = self.RETRY_BASE_DELAY * (2 ** attempt)
                _log.warning(
                    f"Network error on {endpoint}: {e}, "
                    f"sleeping {wait}s "
                    f"(attempt {attempt + 1}/{self.MAX_RETRIES})"
                )
                time.sleep(wait)
                continue

        # 所有重试耗尽
        raise last_exception  # type: ignore[misc]

    def _get(self, endpoint: str, params: dict = None, signed: bool = False) -> dict | list:
        """发送 GET 请求"""
        return self._request("GET", endpoint, params=params, signed=signed)

    def _post(self, endpoint: str, params: dict = None) -> dict | list:
        """发送签名 POST 请求"""
        return self._request("POST", endpoint, params=params, signed=True)

    def _delete(self, endpoint: str, params: dict = None) -> dict | list:
        """发送签名 DELETE 请求"""
        return self._request("DELETE", endpoint, params=params, signed=True)

    # ============================================================
    #  公开接口 (Market Data - 无需 API Key)
    # ============================================================

    def get_exchange_info(self) -> dict:
        """获取交易所信息和所有期权合约列表"""
        return self._get("/eapi/v1/exchangeInfo")

    def get_option_symbols(self, underlying: str = None) -> list[dict]:
        """获取期权合约列表，可按标的资产筛选 (如 'BTCUSDT')"""
        info = self.get_exchange_info()
        symbols = info.get("optionSymbols", [])
        if underlying:
            symbols = [s for s in symbols if s.get("underlying") == underlying]
        return symbols

    def get_ticker(self, symbol: str = None) -> list | dict:
        """获取期权24小时行情数据，不传 symbol 返回全部"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/eapi/v1/ticker", params=params)

    def get_klines(self, symbol: str, interval: str = "1h",
                   start_time: int = None, end_time: int = None, limit: int = 500) -> list:
        """
        获取K线数据
        interval: 1m,3m,5m,15m,30m,1h,2h,4h,6h,12h,1d,3d,1w
        """
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return self._get("/eapi/v1/klines", params=params)

    def get_depth(self, symbol: str, limit: int = 100) -> dict:
        """获取订单簿深度 (limit: 10,20,50,100,500,1000)"""
        return self._get("/eapi/v1/depth", params={"symbol": symbol, "limit": limit})

    def get_recent_trades(self, symbol: str, limit: int = 100) -> list:
        """获取最近成交记录 (limit 最大500)"""
        return self._get("/eapi/v1/trades", params={"symbol": symbol, "limit": limit})

    def get_mark_price(self, symbol: str = None) -> list | dict:
        """获取标记价格 + 希腊值(IV, delta, gamma, theta, vega)"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/eapi/v1/mark", params=params)

    def get_index_price(self, underlying: str) -> dict:
        """获取标的资产指数价格 (如 'BTCUSDT')"""
        return self._get("/eapi/v1/index", params={"underlying": underlying})

    def get_historical_trades(self, symbol: str, from_id: int = None, limit: int = 100) -> list:
        """获取历史成交记录"""
        params = {"symbol": symbol, "limit": limit}
        if from_id:
            params["fromId"] = from_id
        return self._get("/eapi/v1/historicalTrades", params=params)

    def get_open_interest(self, underlying_asset: str, expiration: str) -> list:
        """获取未平仓合约数 (underlying_asset: 'BTC', expiration: '250620')"""
        return self._get("/eapi/v1/openInterest",
                         params={"underlyingAsset": underlying_asset, "expiration": expiration})

    # ============================================================
    #  私有接口 (需要 API Key + Secret)
    # ============================================================

    def get_account(self) -> dict:
        """获取账户信息（余额等）- 注意: 此端点可能不可用"""
        return self._get("/eapi/v1/account", signed=True)

    def get_bill(self, currency: str = None, record_id: int = None, limit: int = 100) -> list:
        """
        获取资金流水 (账单)
        :param currency: 币种，如 'USDT'
        :param record_id: 起始记录ID
        :param limit: 数量限制，默认100，最大1000
        """
        params = {"limit": limit}
        if currency:
            params["currency"] = currency
        if record_id:
            params["recordId"] = record_id
        return self._get("/eapi/v1/bill", params=params, signed=True)

    def get_position(self, symbol: str = None) -> list:
        """获取当前持仓"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/eapi/v1/position", params=params, signed=True)

    def place_order(
        self,
        symbol: str,
        side: str,
        type_: str = "LIMIT",
        quantity: float = None,
        price: float = None,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        post_only: bool = False,
        client_order_id: str = None,
    ) -> dict:
        """
        下单
        :param symbol: 合约名称，如 'BTC-250620-100000-C'
        :param side: 'BUY' 或 'SELL'
        :param type_: 'LIMIT' 或 'MARKET'
        :param quantity: 数量
        :param price: 价格 (LIMIT 单必填)
        :param time_in_force: 'GTC'(默认), 'IOC', 'FOK'
        :param reduce_only: 是否仅减仓
        :param post_only: 是否仅挂单
        :param client_order_id: 自定义订单ID
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": type_,
        }
        if quantity is not None:
            params["quantity"] = str(quantity)
        if price is not None:
            params["price"] = str(price)
        if type_ == "LIMIT":
            params["timeInForce"] = time_in_force
        if reduce_only:
            params["reduceOnly"] = "true"
        if post_only:
            params["postOnly"] = "true"
        if client_order_id:
            params["clientOrderId"] = client_order_id
        return self._post("/eapi/v1/order", params=params)

    def cancel_order(self, symbol: str, order_id: int = None, client_order_id: str = None) -> dict:
        """
        撤单
        :param symbol: 合约名称
        :param order_id: 订单ID (和 client_order_id 二选一)
        :param client_order_id: 自定义订单ID
        """
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["clientOrderId"] = client_order_id
        return self._delete("/eapi/v1/order", params=params)

    def cancel_all_orders(self, symbol: str) -> dict:
        """撤销某个合约的所有挂单"""
        return self._delete("/eapi/v1/allOpenOrders", params={"symbol": symbol})

    def get_open_orders(self, symbol: str = None) -> list:
        """获取当前挂单"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/eapi/v1/openOrders", params=params, signed=True)

    def get_order(self, symbol: str, order_id: int = None, client_order_id: str = None) -> dict:
        """查询单个订单"""
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["clientOrderId"] = client_order_id
        return self._get("/eapi/v1/order", params=params, signed=True)

    def get_history_orders(self, symbol: str, start_time: int = None,
                           end_time: int = None, limit: int = 100) -> list:
        """获取历史委托 (需要指定 symbol)"""
        params = {"symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return self._get("/eapi/v1/historyOrders", params=params, signed=True)

    def get_user_trades(self, symbol: str = None, start_time: int = None,
                        end_time: int = None, limit: int = 100,
                        from_id: int = None) -> list:
        """获取成交历史"""
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if from_id:
            params["fromId"] = from_id
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return self._get("/eapi/v1/userTrades", params=params, signed=True)


# ========================
# 辅助函数
# ========================

def get_account_equity(api: "BinanceOptionsAPI") -> dict:
    """
    统一的账户权益获取函数 (单一事实来源)

    优先使用 /eapi/v1/marginAccount API 直读 (精确),
    fallback 用 get_bill() 流水累加 (不可靠, 超 200 条会截断)。

    Returns:
        {
            "equity": float,            # 账户权益 (含浮盈亏)
            "margin_balance": float,    # 保证金余额 (现金)
            "available": float,         # 可用保证金
            "unrealized_pnl": float,    # 未实现盈亏
            "initial_margin": float,    # 已用初始保证金
            "maint_margin": float,      # 维持保证金
            "source": str,              # "api" 或 "bill_fallback"
        }
    """
    # 优先: /eapi/v1/marginAccount (经验证可用且精确)
    try:
        raw = api._get("/eapi/v1/marginAccount", signed=True)
        asset_list = raw.get("asset", [])
        if isinstance(asset_list, list) and asset_list:
            a = asset_list[0]
            return {
                "equity": float(a.get("equity", 0)),
                "margin_balance": float(a.get("marginBalance", 0)),
                "available": float(a.get("available", 0)),
                "unrealized_pnl": float(a.get("unrealizedPNL", 0)),
                "initial_margin": float(a.get("initialMargin", 0)),
                "maint_margin": float(a.get("maintMargin", 0)),
                "source": "api",
            }
    except Exception as e:
        _log.warning(f"marginAccount API 失败, 降级到流水法: {e}")

    # Fallback: get_bill() 流水累加 (不可靠, 仅在 API 不可用时使用)
    _log.warning("使用 get_bill() 流水法估算余额 — 结果可能不准确(流水超200条会截断)")
    try:
        bills = api.get_bill(currency="USDT", limit=1000)
        total = sum(float(b.get("amount", 0)) for b in bills)
        return {
            "equity": total,
            "margin_balance": total,
            "available": total * 0.5,       # 粗略估算
            "unrealized_pnl": 0,
            "initial_margin": 0,
            "maint_margin": 0,
            "source": "bill_fallback",
        }
    except Exception as e2:
        _log.error(f"get_bill() 也失败了: {e2}")
        return {
            "equity": 0, "margin_balance": 0, "available": 0,
            "unrealized_pnl": 0, "initial_margin": 0, "maint_margin": 0,
            "source": "error",
        }


def ts_to_str(ts_ms: int) -> str:
    """毫秒时间戳转可读时间"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_kline(kline: list) -> dict:
    """解析K线原始数组为字典"""
    return {
        "open_time": ts_to_str(kline[0]),
        "open": kline[1],
        "high": kline[2],
        "low": kline[3],
        "close": kline[4],
        "volume": kline[5],
        "close_time": ts_to_str(kline[6]),
        "amount": kline[7],
        "trades_count": kline[8],
        "taker_buy_volume": kline[9],
        "taker_buy_amount": kline[10],
    }


# ========================
# 演示
# ========================
def main():
    api = BinanceOptionsAPI()

    print("=" * 70)
    print("  币安期权(European Options) 数据获取")
    print("=" * 70)

    # ------- 公开接口 -------

    # 1. 交易所信息
    print("\n[1] 交易所信息 & 合约列表")
    print("-" * 50)
    info = api.get_exchange_info()
    all_symbols = info.get("optionSymbols", [])
    print(f"  总合约数量: {len(all_symbols)}")

    underlying_count = {}
    for s in all_symbols:
        u = s.get("underlying", "UNKNOWN")
        underlying_count[u] = underlying_count.get(u, 0) + 1
    print("  按标的资产分布:")
    for u, cnt in sorted(underlying_count.items()):
        print(f"    {u}: {cnt} 个合约")

    btc_symbols = [s for s in all_symbols if s.get("underlying") == "BTCUSDT"]
    print(f"\n  BTC 期权合约数: {len(btc_symbols)}")
    if btc_symbols:
        sample = btc_symbols[0]
        print(f"  示例合约: {sample['symbol']}")
        print(f"    标的: {sample.get('underlying')}")
        print(f"    行权价: {sample.get('strikePrice')}")
        print(f"    到期日: {ts_to_str(sample.get('expiryDate', 0))}")
        print(f"    类型: {'看涨(Call)' if sample.get('side') == 'CALL' else '看跌(Put)'}")

    # 2. Ticker
    print("\n[2] 24小时行情 (Ticker)")
    print("-" * 50)
    tickers = api.get_ticker()
    print(f"  Ticker 总数: {len(tickers)}")
    active_tickers = sorted(tickers, key=lambda x: float(x.get("volume", 0)), reverse=True)
    print("  交易量 Top 5:")
    for t in active_tickers[:5]:
        print(f"    {t['symbol']}")
        print(f"      最新价: {t['lastPrice']}, 涨跌: {t['priceChangePercent']}%")
        print(f"      成交量: {t['volume']}, 成交额: {t['amount']}")
        print(f"      买一: {t['bidPrice']}, 卖一: {t['askPrice']}")
        print(f"      行权价: {t['strikePrice']}")

    demo_symbol = None
    for t in active_tickers:
        if float(t.get("volume", 0)) > 0:
            demo_symbol = t["symbol"]
            break
    if not demo_symbol and btc_symbols:
        demo_symbol = btc_symbols[0]["symbol"]

    if demo_symbol:
        print(f"\n  (以下用 {demo_symbol} 做演示)")

        # 3. K线
        print(f"\n[3] K线数据 ({demo_symbol}, 1h)")
        print("-" * 50)
        try:
            klines = api.get_klines(demo_symbol, interval="1h", limit=5)
            for k in klines:
                parsed = parse_kline(k)
                print(f"    {parsed['open_time']} | "
                      f"O:{parsed['open']} H:{parsed['high']} L:{parsed['low']} C:{parsed['close']} | "
                      f"Vol:{parsed['volume']}")
        except Exception as e:
            print(f"  获取K线失败: {e}")

        # 4. 深度
        print(f"\n[4] 订单簿深度 ({demo_symbol})")
        print("-" * 50)
        try:
            depth = api.get_depth(demo_symbol, limit=10)
            print("  买盘 (Bids):")
            for bid in depth.get("bids", [])[:5]:
                print(f"    价格: {bid[0]}, 数量: {bid[1]}")
            print("  卖盘 (Asks):")
            for ask in depth.get("asks", [])[:5]:
                print(f"    价格: {ask[0]}, 数量: {ask[1]}")
        except Exception as e:
            print(f"  获取深度失败: {e}")

        # 5. 最近成交
        print(f"\n[5] 最近成交 ({demo_symbol})")
        print("-" * 50)
        try:
            trades = api.get_recent_trades(demo_symbol, limit=5)
            for t in trades:
                print(f"    {ts_to_str(t['time'])} | 价格: {t['price']}, 数量: {t['qty']}, "
                      f"方向: {'买' if t.get('side', '') == 'BUY' else '卖'}")
        except Exception as e:
            print(f"  获取成交失败: {e}")

    # 6. 标记价格
    print("\n[6] 标记价格 (Mark Price)")
    print("-" * 50)
    marks = api.get_mark_price()
    print(f"  标记价格总数: {len(marks)}")
    mark_with_price = [m for m in marks if float(m.get("markPrice", 0)) > 0]
    print(f"  有标记价格的合约: {len(mark_with_price)}")
    for m in mark_with_price[:5]:
        print(f"    {m['symbol']}: markPrice={m['markPrice']}, "
              f"bidIV={m.get('bidIV', 'N/A')}, askIV={m.get('askIV', 'N/A')}, "
              f"delta={m.get('delta', 'N/A')}, theta={m.get('theta', 'N/A')}, "
              f"gamma={m.get('gamma', 'N/A')}, vega={m.get('vega', 'N/A')}")

    # 7. 指数价格
    print("\n[7] 标的指数价格")
    print("-" * 50)
    for underlying in ["BTCUSDT", "ETHUSDT"]:
        try:
            idx = api.get_index_price(underlying)
            print(f"  {underlying}: indexPrice = {idx.get('indexPrice')}, "
                  f"time = {ts_to_str(idx.get('time', 0))}")
        except Exception as e:
            print(f"  {underlying}: 获取失败 - {e}")

    # 8. Open Interest
    print("\n[8] 未平仓合约 (Open Interest)")
    print("-" * 50)
    if btc_symbols:
        expirations = set()
        for s in btc_symbols:
            parts = s["symbol"].split("-")
            if len(parts) >= 2:
                expirations.add(parts[1])
        expirations = sorted(expirations)
        if expirations:
            exp = expirations[0]
            print(f"  BTC 到期日 {exp} 的 Open Interest:")
            try:
                oi_list = api.get_open_interest("BTC", exp)
                for oi in oi_list[:10]:
                    print(f"    {oi['symbol']}: OI={oi.get('sumOpenInterest', 'N/A')}, "
                          f"sumOpenInterestUsd={oi.get('sumOpenInterestUsd', 'N/A')}")
            except Exception as e:
                print(f"  获取OI失败: {e}")

    # ------- 私有接口 (需要 API Key) -------
    if api.api_key and api.secret_key:
        print("\n" + "=" * 70)
        print("  私有接口 (鉴权)")
        print("=" * 70)

        # 9. 资金流水 (Bill)
        print("\n[9] 资金流水 (Bill)")
        print("-" * 50)
        try:
            bills = api.get_bill(limit=10)
            if bills:
                for b in bills:
                    print(f"    {ts_to_str(b.get('createDate', 0))} | "
                          f"{b.get('asset')}: {b.get('amount')} | "
                          f"类型: {b.get('type')}")
            else:
                print("  (暂无流水记录)")
        except Exception as e:
            print(f"  获取资金流水失败: {e}")

        # 10. 当前持仓
        print("\n[10] 当前持仓")
        print("-" * 50)
        try:
            positions = api.get_position()
            active_pos = [p for p in positions if float(p.get("quantity", 0)) != 0]
            if active_pos:
                for p in active_pos:
                    print(f"    {p['symbol']}: "
                          f"数量={p.get('quantity')}, "
                          f"入场价={p.get('entryPrice')}, "
                          f"标记价={p.get('markPrice')}, "
                          f"未实现盈亏={p.get('unrealizedPnL')}")
            else:
                print("  (暂无持仓)")
        except Exception as e:
            print(f"  获取持仓失败: {e}")

        # 11. 当前挂单
        print("\n[11] 当前挂单")
        print("-" * 50)
        try:
            open_orders = api.get_open_orders()
            if open_orders:
                for o in open_orders[:10]:
                    print(f"    {o['symbol']}: "
                          f"{o.get('side')} {o.get('type')} "
                          f"价格={o.get('price')}, 数量={o.get('quantity')}, "
                          f"状态={o.get('status')}")
            else:
                print("  (暂无挂单)")
        except Exception as e:
            print(f"  获取挂单失败: {e}")

        # 12. 历史委托
        print("\n[12] 历史委托 (最近10条)")
        print("-" * 50)
        try:
            # 从持仓或成交记录中获取一个已知 symbol
            history_symbol = None
            try:
                positions = api.get_position()
                for p in positions:
                    if float(p.get("quantity", 0)) != 0:
                        history_symbol = p["symbol"]
                        break
            except Exception:
                pass
            if not history_symbol and btc_symbols:
                history_symbol = btc_symbols[0]["symbol"]
            history = api.get_history_orders(symbol=history_symbol, limit=10) if history_symbol else []
            if history:
                for o in history:
                    print(f"    {o.get('symbol')}: "
                          f"{o.get('side')} {o.get('type')} "
                          f"价格={o.get('price')}, 数量={o.get('quantity')}, "
                          f"状态={o.get('status')}, "
                          f"时间={ts_to_str(o.get('createTime', 0))}")
            else:
                print("  (暂无历史委托)")
        except Exception as e:
            print(f"  获取历史委托失败: {e}")

        # 13. 成交历史
        print("\n[13] 成交历史 (最近10条)")
        print("-" * 50)
        try:
            user_trades = api.get_user_trades(limit=10)
            if user_trades:
                for t in user_trades:
                    print(f"    {t.get('symbol')}: "
                          f"{t.get('side')} 价格={t.get('price')}, 数量={t.get('quantity')}, "
                          f"手续费={t.get('fee')}, "
                          f"时间={ts_to_str(t.get('time', 0))}")
            else:
                print("  (暂无成交记录)")
        except Exception as e:
            print(f"  获取成交历史失败: {e}")
    else:
        print("\n  [提示] 未配置 API Key，跳过私有接口。")
        print("  请在 .env 文件中设置 BINANCE_API_KEY 和 BINANCE_SECRET_KEY")

    print("\n" + "=" * 70)
    print("  全部数据获取完成!")
    print("=" * 70)


if __name__ == "__main__":
    main()
