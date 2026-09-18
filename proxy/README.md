# Proxy failover (Binance egress)

Local `sing-box` listens on `127.0.0.1:7890`. Set `BINANCE_PROXY_URL` to your subscription URL in `.env` (never commit it).

```bash
# one-shot reprobe
./.venv/bin/python3 proxy/proxy_failover.py once

# health check
./.venv/bin/python3 proxy/proxy_failover.py check

# continuous failover (also started by proxy_watchdog.sh / start_bot.sh)
./.venv/bin/python3 proxy/proxy_failover.py watch
```

On two consecutive failed health checks against Binance eAPI via the local proxy, the watcher refreshes the subscription, probes each node, and pins the first that returns HTTP 200.
