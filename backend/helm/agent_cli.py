"""``helm-agent`` — the Agent Experience Interface (AXI) for Helm.

A token-efficient CLI that a live agent (Claude Code, Codex, etc.) drives to
operate the entire Helm stack: trading state, order submission, exotic feeds,
OpenBB research, and event-driven wait. Output uses TOON for ~40% token
savings vs JSON; pass ``--json`` for raw machine-readable output.

The CLI talks only to the Helm HTTP API (default ``http://127.0.0.1:8000``).
Override with ``HELM_API_URL``. No imports from ``helm.*`` runtime modules so
this stays operable even when the backend is half-broken.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

VERSION = "0.1.0"
DEFAULT_BASE = os.environ.get("HELM_API_URL", "http://127.0.0.1:8000")
WS_BASE = DEFAULT_BASE.replace("http://", "ws://").replace("https://", "wss://")
DESC = "Operate the Helm AI-trading workspace (trading, feeds, OpenBB, Nautilus) from one CLI."

# ----------------------------------------------------------------------------- #
# TOON encoder
# ----------------------------------------------------------------------------- #

def _toon_scalar(v: Any) -> str:
    """Encode a scalar; quote strings that contain commas/colons/spaces."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:  # NaN
            return "null"
        return f"{v}"
    s = str(v)
    if any(c in s for c in (",", ":", "\n", "[", "]", "{", "}")) or (s and s[0] in " \t"):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _toon_array_of_dicts(name: str, items: list[dict], indent: int = 0) -> str:
    if not items:
        return f"{'  ' * indent}{name}[0]: (empty)"
    # Use the union of keys from the first item as the schema.
    fields = list(items[0].keys())
    head = f"{'  ' * indent}{name}[{len(items)}]{{{','.join(fields)}}}:"
    rows = []
    for it in items:
        rows.append(
            f"{'  ' * (indent + 1)}" + ",".join(_toon_scalar(it.get(k)) for k in fields)
        )
    return head + "\n" + "\n".join(rows)


def _toon_dict(d: dict, indent: int = 0) -> str:
    out: list[str] = []
    for k, v in d.items():
        prefix = "  " * indent
        if isinstance(v, dict):
            out.append(f"{prefix}{k}:")
            out.append(_toon_dict(v, indent + 1))
        elif isinstance(v, list):
            if not v:
                out.append(f"{prefix}{k}: 0 (empty)")
            elif isinstance(v[0], dict):
                out.append(_toon_array_of_dicts(k, v, indent))
            else:
                out.append(f"{prefix}{k}[{len(v)}]: " + ", ".join(_toon_scalar(x) for x in v))
        else:
            out.append(f"{prefix}{k}: {_toon_scalar(v)}")
    return "\n".join(out)


def toon(obj: Any) -> str:
    """Serialize a dict (top-level) to TOON text."""
    if obj is None:
        return ""
    if isinstance(obj, list):
        return _toon_array_of_dicts("items", obj, 0) if obj and isinstance(obj[0], dict) else (
            "items[" + str(len(obj)) + "]: " + ", ".join(_toon_scalar(x) for x in obj)
        )
    if isinstance(obj, dict):
        return _toon_dict(obj, 0)
    return _toon_scalar(obj)


# ----------------------------------------------------------------------------- #
# HTTP helpers
# ----------------------------------------------------------------------------- #

def _client() -> httpx.Client:
    return httpx.Client(base_url=DEFAULT_BASE, timeout=30.0)


def _request(method: str, path: str, **kw: Any) -> Any:
    with _client() as c:
        r = c.request(method, path, **kw)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            _emit({"error": f"{method} {path} -> {r.status_code}", "detail": r.text[:500]})
            sys.exit(1)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text


# ----------------------------------------------------------------------------- #
# Output / suggestions
# ----------------------------------------------------------------------------- #

USE_JSON = False


def _emit(obj: Any, suggestions: list[str] | None = None) -> None:
    if USE_JSON:
        out: dict[str, Any] = {"data": obj}
        if suggestions:
            out["help"] = suggestions
        print(json.dumps(out, separators=(",", ":"), default=str))
        return
    print(toon(obj))
    if suggestions:
        print()
        print(f"help[{len(suggestions)}]:")
        for s in suggestions:
            print(f"  {s}")


def _trunc(s: str | None, n: int = 800) -> dict[str, Any]:
    if s is None:
        return {"value": None, "size": 0}
    if len(s) <= n:
        return {"value": s, "size": len(s)}
    return {"value": s[:n] + "…", "size": len(s), "truncated": True}


# ----------------------------------------------------------------------------- #
# Read commands
# ----------------------------------------------------------------------------- #

def cmd_status(_: argparse.Namespace) -> None:
    """One-shot snapshot of the whole helm system — what an agent sees at session start."""
    health = _request("GET", "/api/health") or {}
    ai = _request("GET", "/api/ai/status") or {}
    portfolio = _request("GET", "/api/trading/portfolio") or {}
    positions = _request("GET", "/api/trading/positions") or []
    orders = _request("GET", "/api/trading/orders") or []
    instruments = _request("GET", "/api/trading/instruments") or []
    decisions = _request("GET", "/api/ai/decisions?limit=5") or []
    pending = _request("GET", "/api/agent/pending") or {}

    # Active OpenBB probe — health flag only checks the local import.
    openbb_url = os.environ.get("HELM_OPENBB_URL", "http://localhost:6900")
    openbb_reachable = False
    try:
        with httpx.Client(timeout=2.0) as c:
            openbb_reachable = c.get(openbb_url + "/openapi.json").status_code == 200
    except Exception:
        openbb_reachable = False

    bin_path = shutil.which("helm-agent") or sys.argv[0]
    home = str(Path.home())
    if bin_path.startswith(home):
        bin_path = "~" + bin_path[len(home):]
    snap: dict[str, Any] = {
        "bin": bin_path,
        "description": DESC,
        "api": DEFAULT_BASE,
        "pending_wakes": pending.get("count", 0),
        "engine": {
            "mode": health.get("mode"),
            "running": health.get("engine_running"),
            "nautilus": health.get("nautilus_available"),
            "openbb_lib": health.get("openbb_available"),
            "openbb_reachable": openbb_reachable,
            "version": health.get("version"),
        },
        "ai_trader": {
            "state": ai.get("state"),
            "enabled": ai.get("enabled"),
            "strategy": ai.get("strategy_name"),
            "decisions_today": ai.get("decisions_today"),
            "win_rate": ai.get("win_rate"),
        },
        "portfolio": {
            "equity": portfolio.get("equity"),
            "realized_pnl": portfolio.get("realized_pnl"),
            "unrealized_pnl": portfolio.get("unrealized_pnl"),
            "net_exposure": portfolio.get("net_exposure"),
        },
        "instruments": [
            {"id": i["id"], "symbol": i["symbol"], "venue": i["venue"]} for i in instruments
        ],
        "positions": [
            {"id": p["id"], "instrument": p["instrument"], "side": p["side"],
             "qty": p["quantity"], "avg_px": p["avg_px"], "upnl": p["unrealized_pnl"]}
            for p in positions
        ],
        "orders": [
            {"id": o["id"], "instrument": o["instrument"], "side": o["side"],
             "qty": o["quantity"], "filled": o["filled_qty"], "status": o["status"]}
            for o in orders[:10]
        ],
        "recent_decisions": [
            {"ts": d["ts"][:19], "action": d["action"], "instrument": d["instrument"],
             "conf": d["confidence"], "status": d["status"]}
            for d in decisions
        ],
    }
    _emit(snap, suggestions=[
        "helm-agent bars <instrument>           # candles for one instrument",
        "helm-agent submit <id> BUY <qty>       # market order",
        "helm-agent feeds                       # list feed sources",
        "helm-agent sleep --on-event order      # block until next order event",
        "helm-agent <cmd> --help                # per-command reference",
    ])


def cmd_positions(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/trading/positions") or []
    items = [{"id": p["id"], "instrument": p["instrument"], "side": p["side"],
              "qty": p["quantity"], "avg_px": p["avg_px"], "upnl": p["unrealized_pnl"]}
             for p in data]
    _emit({"positions_count": len(items), "positions": items},
          suggestions=["helm-agent close <instrument>          # flatten one position"] if items
          else ["helm-agent submit <id> BUY <qty>      # open one"])


def cmd_orders(args: argparse.Namespace) -> None:
    data = _request("GET", "/api/trading/orders") or []
    rows = [{"id": o["id"], "ts": o["ts"][:19], "instrument": o["instrument"],
             "side": o["side"], "type": o["type"], "qty": o["quantity"],
             "filled": o["filled_qty"], "status": o["status"]} for o in data[: args.limit]]
    _emit({"orders_count": f"{len(rows)} of {len(data)}", "orders": rows})


def cmd_accounts(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/trading/account") or []
    _emit({"accounts": data})


def cmd_portfolio(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/trading/portfolio") or {}
    _emit({
        "equity": data.get("equity"),
        "realized_pnl": data.get("realized_pnl"),
        "unrealized_pnl": data.get("unrealized_pnl"),
        "net_exposure": data.get("net_exposure"),
        "positions": [
            {"instrument": p["instrument"], "side": p["side"], "qty": p["quantity"],
             "upnl": p["unrealized_pnl"]}
            for p in data.get("positions", [])
        ],
    })


def cmd_instruments(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/trading/instruments") or []
    rows = [{"id": i["id"], "symbol": i["symbol"], "venue": i["venue"],
             "asset_class": i["asset_class"], "ccy": i["quote_currency"]} for i in data]
    _emit({"instruments_count": len(rows), "instruments": rows},
          suggestions=["helm-agent bars <id>                   # fetch 1-min OHLCV"])


_INTRADAY_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
_DAILY_TIMEFRAMES = {"1d": "1d", "1w": "1W", "1M": "1M"}


def _aggregate_minute_bars(bars: list[dict], mins_per_bar: int) -> list[dict]:
    """Roll 1-min OHLCV bars up to N-minute buckets."""
    if mins_per_bar <= 1:
        return bars
    bucket_ms = mins_per_bar * 60_000
    out: list[dict] = []
    cur: dict | None = None
    cur_bucket = -1
    from datetime import datetime as _dt
    for b in bars:
        bts_ms = int(_dt.fromisoformat(b["ts"].replace("Z", "+00:00")).timestamp() * 1000)
        bucket = bts_ms // bucket_ms
        if bucket != cur_bucket:
            if cur is not None:
                out.append(cur)
            cur = {**b, "ts": _dt.fromtimestamp((bucket * bucket_ms) / 1000).isoformat() + "Z"}
            cur_bucket = bucket
        else:
            assert cur is not None
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b["volume"]
    if cur is not None:
        out.append(cur)
    return out


def cmd_bars(args: argparse.Namespace) -> None:
    """Fetch OHLCV bars at any timeframe.

    1m through 4h: aggregate from Nautilus' cached 1-min bars (live + recent).
    1d, 1w, 1M:   pull from OpenBB historical (yfinance/fmp/polygon).
    """
    tf = args.timeframe
    instr = args.instrument

    if tf in _INTRADAY_MINUTES:
        # Ask for enough 1-min bars to fill `count` aggregated bars.
        raw_count = min(500, args.count * _INTRADAY_MINUTES[tf])
        raw = _request("GET", f"/api/trading/bars?instrument={instr}&count={raw_count}") or []
        bars = _aggregate_minute_bars(raw, _INTRADAY_MINUTES[tf])[-args.count:]
        source = "nautilus-cache"

    elif tf in _DAILY_TIMEFRAMES:
        from datetime import date, timedelta
        # ~count + slack to cover weekends + holidays.
        days_back = max(args.count * (7 if tf == "1w" else 1) * 2, 30)
        end = date.today()
        start = end - timedelta(days=days_back)
        ob = _request("POST", "/api/agent/openbb", json={
            "path": "/api/v1/equity/price/historical",
            "params": {
                "symbol": instr.split(".")[0],
                "provider": args.provider,
                "interval": _DAILY_TIMEFRAMES[tf],
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        }) or {}
        rows = (ob.get("results") if isinstance(ob, dict) else None) or []
        # Normalize to the same {ts, open, high, low, close, volume} shape
        # the intraday path uses, so downstream rendering stays uniform.
        bars = [
            {
                "ts": (r.get("date") or "") + ("T00:00:00Z" if "T" not in (r.get("date") or "") else ""),
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "volume": r.get("volume") or 0,
            }
            for r in rows
            if all(k in r for k in ("open", "high", "low", "close"))
        ][-args.count:]
        source = f"openbb:{args.provider}"

    else:
        _emit({"error": f"unknown --timeframe {tf!r}",
               "help": "supported: " + ", ".join(list(_INTRADAY_MINUTES) + list(_DAILY_TIMEFRAMES))})
        sys.exit(2)

    if not bars:
        _emit({"instrument": instr, "timeframe": tf, "bars_count": 0,
               "source": source,
               "note": "no bars returned — for intraday try after market open; for "
                       "daily/weekly check that OpenBB is reachable and the symbol is valid"},
              suggestions=["helm-agent status                      # check openbb_reachable",
                           "helm-agent instruments                 # confirm loaded instruments"])
        return

    rows = [{"ts": b["ts"][:19].replace("T", " "), "o": b["open"], "h": b["high"],
             "l": b["low"], "c": b["close"], "v": b["volume"]} for b in bars]
    _emit({
        "instrument": instr, "timeframe": tf, "source": source,
        "bars_count": len(rows),
        "from": bars[0]["ts"][:19], "to": bars[-1]["ts"][:19],
        "bars": rows,
    })


def cmd_decisions(args: argparse.Namespace) -> None:
    data = _request("GET", f"/api/ai/decisions?limit={args.limit}") or []
    rows = [{"id": d["id"], "ts": d["ts"][:19], "action": d["action"],
             "instrument": d["instrument"], "conf": d["confidence"], "status": d["status"],
             "thesis": _trunc(d.get("thesis"), 120)["value"]} for d in data]
    _emit({"decisions_count": len(rows), "decisions": rows})


def cmd_feeds(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/feeds/sources") or []
    _emit({"sources_count": len(data), "sources": data},
          suggestions=["helm-agent feed <source>                # fetch normalized FeedItems"])


def cmd_feed(args: argparse.Namespace) -> None:
    q = f"limit={args.limit}"
    for kv in (args.param or []):
        if "=" not in kv:
            _emit({"error": f"bad --param: {kv}", "help": "use key=value"})
            sys.exit(2)
        q += "&" + kv
    if args.query:
        q += f"&query={args.query}"
    data = _request("GET", f"/api/feeds/{args.source}?{q}") or []
    rows = [{"id": item.get("id"), "ts": (item.get("ts") or "")[:19],
             "title": _trunc(item.get("title"), 100)["value"],
             "url": item.get("url"), "sentiment": item.get("sentiment")} for item in data]
    _emit({"source": args.source, "items_count": len(rows), "items": rows})


def cmd_oembed(args: argparse.Namespace) -> None:
    data = _request("GET", f"/api/feeds/oembed?url={args.url}") or {}
    _emit(data)


def cmd_news(args: argparse.Namespace) -> None:
    if args.symbol:
        path = "/api/v1/news/company"
        params = {"symbol": args.symbol, "provider": args.provider or "yfinance", "limit": args.limit}
    else:
        path = "/api/v1/news/world"
        params = {"provider": args.provider or "yfinance", "limit": args.limit}
    data = _request("POST", "/api/agent/openbb", json={"path": path, "params": params}) or {}
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        _emit({"news": data})
        return
    rows = [{"ts": (n.get("date") or "")[:19], "title": _trunc(n.get("title"), 110)["value"],
             "source": n.get("source"), "url": n.get("url")} for n in results]
    _emit({"news_count": len(rows), "news": rows})


def cmd_openbb(args: argparse.Namespace) -> None:
    params: dict[str, str] = {}
    for kv in (args.param or []):
        if "=" not in kv:
            _emit({"error": f"bad --param: {kv}", "help": "use key=value"})
            sys.exit(2)
        k, _, v = kv.partition("=")
        params[k] = v
    data = _request("POST", "/api/agent/openbb", json={"path": args.path, "params": params}) or {}
    _emit(data)


# ----------------------------------------------------------------------------- #
# Write commands
# ----------------------------------------------------------------------------- #

def cmd_submit(args: argparse.Namespace) -> None:
    body = {"instrument": args.instrument, "side": args.side.upper(),
            "quantity": args.quantity, "type": "LIMIT" if args.limit else "MARKET"}
    if args.limit:
        body["price"] = args.limit
    order = _request("POST", "/api/agent/orders", json=body)
    if order is None:
        sys.exit(1)
    _emit({"submitted": order},
          suggestions=[f"helm-agent cancel {order.get('id')}",
                       f"helm-agent close {args.instrument}"])


def cmd_cancel(args: argparse.Namespace) -> None:
    data = _request("DELETE", f"/api/agent/orders/{args.order_id}") or {}
    if data.get("cancelled"):
        _emit({"order_id": args.order_id, "cancelled": True})
    else:
        _emit({"order_id": args.order_id, "cancelled": False,
               "note": "order not found or already in a terminal state (no-op)"})


def cmd_close(args: argparse.Namespace) -> None:
    data = _request("POST", f"/api/agent/close/{args.instrument}") or {}
    if data.get("closed"):
        _emit({"closed": True, "instrument": args.instrument, "order": data.get("order")})
    else:
        _emit({"closed": False, "instrument": args.instrument,
               "note": "no open position to close (no-op)"})


def cmd_close_all(args: argparse.Namespace) -> None:
    """Flatten every open position. Pass --exclude id,id to keep some."""
    excluded = set((args.exclude or "").split(",")) if args.exclude else set()
    excluded.discard("")
    positions = _request("GET", "/api/trading/positions") or []
    targets = [p["instrument"] for p in positions if p["instrument"] not in excluded]
    if not targets:
        _emit({"closed": [], "note": "no open positions" if not positions else "all excluded"})
        return
    results = []
    for instr in targets:
        data = _request("POST", f"/api/agent/close/{instr}") or {}
        results.append({"instrument": instr, "closed": bool(data.get("closed")),
                        "order_id": (data.get("order") or {}).get("id")})
    _emit({"requested": len(targets), "closed": results,
           "skipped": sorted(excluded) if excluded else None})


def cmd_wait_fill(args: argparse.Namespace) -> None:
    """Block on /ws until ``order_id`` reaches a terminal state (or timeout)."""
    asyncio.run(_wait_fill_async(args))


_TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}


async def _wait_fill_async(args: argparse.Namespace) -> None:
    import websockets

    target = args.order_id
    deadline = asyncio.get_event_loop().time() + float(args.timeout)
    url = WS_BASE + "/ws"

    # 1) Snapshot via REST first — order may already be terminal.
    orders = _request("GET", "/api/trading/orders") or []
    for o in orders:
        if o.get("id") == target and (o.get("status") or "") in _TERMINAL_STATUSES:
            _emit({"order_id": target, "status": o["status"],
                   "filled_qty": o.get("filled_qty"), "avg_px": o.get("avg_px"),
                   "source": "snapshot"})
            return

    # 2) Otherwise subscribe and wait.
    async def _loop():
        async with websockets.connect(url) as ws:
            while True:
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") != "order":
                    continue
                p = msg.get("payload") or {}
                if p.get("id") != target:
                    continue
                if (p.get("status") or "") in _TERMINAL_STATUSES:
                    return {"order_id": target, "status": p["status"],
                            "filled_qty": p.get("filled_qty"), "avg_px": p.get("avg_px"),
                            "source": "ws"}

    try:
        result = await asyncio.wait_for(
            _loop(), timeout=max(0.0, deadline - asyncio.get_event_loop().time()),
        )
    except asyncio.TimeoutError:
        # On timeout, fall back to one more REST snapshot.
        orders = _request("GET", "/api/trading/orders") or []
        for o in orders:
            if o.get("id") == target:
                _emit({"order_id": target, "status": o.get("status"),
                       "filled_qty": o.get("filled_qty"), "avg_px": o.get("avg_px"),
                       "timeout": True, "source": "snapshot-after-timeout"})
                return
        _emit({"order_id": target, "timeout": True, "found": False})
        return
    _emit(result)


def cmd_say(args: argparse.Namespace) -> None:
    """Post a message back to the webui chat panel.

    The message body is taken from:
      * stdin if ``message == "-"``  (e.g. ``some-cmd | helm-agent say -``)
      * the file at ``path`` if ``message == "@path"``
      * the literal ``message`` otherwise

    The stdin/file paths sidestep shell quoting on long markdown replies,
    which was the dominant friction point in agent-driven flows.
    """
    msg = args.message
    if msg == "-":
        msg = sys.stdin.read()
    elif msg.startswith("@"):
        try:
            msg = Path(msg[1:]).read_text()
        except OSError as e:
            _emit({"error": f"could not read {msg[1:]!r}", "detail": str(e)})
            sys.exit(2)
    if not msg.strip():
        _emit({"error": "empty message", "help": 'pass text, "-" for stdin, or "@path" for a file'})
        sys.exit(2)
    body = {"message": msg, "role": args.role}
    data = _request("POST", "/api/agent/say", json=body) or {}
    _emit(
        {"posted": bool(data.get("posted")),
         "ts": (data.get("payload") or {}).get("ts"),
         "acked_wake_id": data.get("acked_wake_id"),
         "chars": len(msg)},
        suggestions=[
            "helm-agent sleep --on-event wake       # RE-ARM so the next webui msg can fire",
        ],
    )


_INSTRUMENT_RE = __import__("re").compile(r"^[A-Z0-9._/-]+\.[A-Z][A-Z0-9_]+$")


def cmd_add_instrument(args: argparse.Namespace) -> None:
    """Add a ticker to HELM_INSTRUMENTS. By default returns a 'restart_required'
    notice; pass --restart to fire POST /api/agent/restart immediately after."""
    iid = args.instrument.strip().upper() if args.upper else args.instrument.strip()
    if not _INSTRUMENT_RE.match(iid):
        _emit({"error": f"invalid instrument id {iid!r}",
               "help": "use SYMBOL.VENUE (e.g. AAPL.NASDAQ, BTCUSDT.BINANCE)"})
        sys.exit(2)
    data = _request("POST", "/api/agent/instruments", json={"id": iid}) or {}
    if args.restart and data.get("restart_required"):
        _emit({"added": data.get("added"), "instruments": data.get("instruments"),
               "restarting": True})
        # Fire-and-forget; the response will arrive before re-exec.
        with contextlib.suppress(Exception):
            _request("POST", "/api/agent/restart")
        return
    _emit(data, suggestions=(
        ["helm-agent restart                     # re-exec uvicorn so the engine sees it"]
        if data.get("restart_required") else None
    ))


def cmd_remove_instrument(args: argparse.Namespace) -> None:
    data = _request("DELETE", f"/api/agent/instruments/{args.instrument}") or {}
    _emit(data, suggestions=(
        ["helm-agent restart"] if data.get("restart_required") else None
    ))


def cmd_restart(_: argparse.Namespace) -> None:
    """Trigger an in-process re-exec of uvicorn so .env changes take effect."""
    data = _request("POST", "/api/agent/restart") or {}
    _emit(data, suggestions=["helm-agent status                      # check the engine after a few seconds"])


def cmd_pending(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/agent/pending") or {}
    _emit(data)


def cmd_decide(args: argparse.Namespace) -> None:
    """Push an AIDecision into the Decisions tab.

    Use this whenever the agent makes a trading judgement worth recording —
    even a HOLD. It is the canonical way to populate the Decisions feed
    while the in-process AIBrain timer is disabled.
    """
    body: dict[str, Any] = {
        "action": args.action.upper(),
        "confidence": args.conf,
        "thesis": args.thesis,
        "status": args.status,
    }
    if args.instrument:
        body["instrument"] = args.instrument
    if args.reasoning:
        if args.reasoning == "-":
            body["reasoning"] = sys.stdin.read()
        elif args.reasoning.startswith("@"):
            body["reasoning"] = Path(args.reasoning[1:]).read_text()
        else:
            body["reasoning"] = args.reasoning
    if args.order_id:
        body["order_id"] = args.order_id
    data = _request("POST", "/api/agent/decisions", json=body) or {}
    _emit({"id": data.get("id"), "action": data.get("action"),
           "instrument": data.get("instrument"), "status": data.get("status"),
           "confidence": data.get("confidence")})


# --- Nautilus artifacts: backtests + risk + strategies ----------------------

def cmd_backtests(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/agent/backtests") or []
    rows = [{
        "id": b["id"], "name": _trunc(b["name"], 60)["value"], "strategy": b["strategy"],
        "start": b["start"][:10], "end": b["end"][:10],
        "ret_pct": b["total_return_pct"], "sharpe": b.get("sharpe"),
        "dd_pct": b.get("max_drawdown_pct"), "trades": b["trades_count"],
    } for b in data]
    _emit({"backtests_count": len(rows), "backtests": rows},
          suggestions=[
              "helm-agent backtest <id>               # full equity curve + trades",
              "helm-agent strategies                  # what backtest harnesses exist",
          ] if rows else None)


def cmd_backtest(args: argparse.Namespace) -> None:
    data = _request("GET", f"/api/agent/backtests/{args.id}") or {}
    if not data:
        return
    eq = data.get("equity_curve") or []
    trades = data.get("trades") or []
    summary = {
        "id": data.get("id"), "name": data.get("name"), "strategy": data.get("strategy"),
        "instruments": data.get("instruments"),
        "period": f"{(data.get('start') or '')[:10]} → {(data.get('end') or '')[:10]}",
        "starting_equity": data.get("starting_equity"),
        "final_equity": data.get("final_equity"),
        "total_return_pct": data.get("total_return_pct"),
        "sharpe": data.get("sharpe"),
        "max_drawdown_pct": data.get("max_drawdown_pct"),
        "trades_count": data.get("trades_count"),
        "notes": _trunc(data.get("notes"), 500)["value"],
        "equity_curve_points": len(eq),
        "trades_shown": min(len(trades), args.trades),
        "trades": [{"ts": t["ts"][:19], "instrument": t["instrument"], "side": t["side"],
                    "qty": t["quantity"], "px": t["price"], "pnl": t.get("pnl")}
                   for t in trades[: args.trades]],
    }
    _emit(summary)


def cmd_risk(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/agent/risk") or []
    rows = [{
        "id": r["id"], "name": _trunc(r["name"], 60)["value"],
        "ts": r["ts"][:19], "equity": r["portfolio_equity"],
        "gross_exp": r["gross_exposure"], "net_exp": r["net_exposure"],
        "var_95": r.get("var_95"),
    } for r in data]
    _emit({"risk_count": len(rows), "risk": rows},
          suggestions=["helm-agent risk-view <id>             # exposures + scenarios"] if rows else None)


def cmd_risk_view(args: argparse.Namespace) -> None:
    data = _request("GET", f"/api/agent/risk/{args.id}") or {}
    if not data:
        return
    _emit({
        "id": data.get("id"), "name": data.get("name"),
        "ts": (data.get("ts") or "")[:19],
        "portfolio_equity": data.get("portfolio_equity"),
        "gross_exposure": data.get("gross_exposure"),
        "net_exposure": data.get("net_exposure"),
        "var_95": data.get("var_95"),
        "notes": _trunc(data.get("notes"), 500)["value"],
        "exposures": data.get("exposures") or [],
        "scenarios": data.get("scenarios") or [],
    })


def cmd_strategies(_: argparse.Namespace) -> None:
    data = _request("GET", "/api/agent/strategies") or []
    rows = [{"id": s["id"], "name": s["name"], "kind": s["kind"],
             "description": _trunc(s["description"], 90)["value"]} for s in data]
    _emit({"strategies_count": len(rows), "strategies": rows})


def cmd_pause(_: argparse.Namespace) -> None:
    data = _request("POST", "/api/ai/control", json={"action": "pause"}) or {}
    _emit({"ai_state": data.get("state"), "enabled": data.get("enabled")})


def cmd_resume(_: argparse.Namespace) -> None:
    data = _request("POST", "/api/ai/control", json={"action": "resume"}) or {}
    _emit({"ai_state": data.get("state"), "enabled": data.get("enabled")})


# ----------------------------------------------------------------------------- #
# Sleep / triggers
# ----------------------------------------------------------------------------- #

_SLEEP_PID_FILE = Path.home() / ".cache" / "helm-agent" / "sleep.pid"


def _existing_sleep_pid() -> int | None:
    """Return the PID of an already-running ``helm-agent sleep`` (if alive)."""
    try:
        raw = _SLEEP_PID_FILE.read_text().strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    try:
        os.kill(pid, 0)  # signal 0 = "are you alive?"
    except OSError:
        return None
    return pid


def cmd_sleep(args: argparse.Namespace) -> None:
    """Block until any configured trigger fires. Prints the trigger result and exits 0.

    Before subscribing to /ws, polls /api/agent/pending — if there are unprocessed
    wakes (delivered while the agent was busy or offline), emit the oldest one
    immediately and exit. Caller can pass --no-pending to skip the queue check
    and only watch live events.

    De-dup: if a previous ``helm-agent sleep`` is still running, this call
    emits an ``already_armed`` status and exits 0 without starting a second
    subscriber. The Stop-hook is still satisfied (the last Bash command WAS
    ``helm-agent sleep --on-event wake``) but we don't accumulate parked
    subscribers across turns. Pass ``--force`` to override.
    """
    if not args.force:
        existing = _existing_sleep_pid()
        if existing is not None:
            _emit(
                {"already_armed": True, "pid": existing,
                 "note": "another helm-agent sleep is already parked on /ws; "
                         "not starting a second one. Pass --force to override."},
                suggestions=[f"kill {existing}  # if you actually want to replace it"],
            )
            return

    if (
        not args.no_pending
        and (args.on_event is None or "wake" in args.on_event.split(","))
    ):
        pending = _request("GET", "/api/agent/pending") or {}
        items = pending.get("messages") or []
        if items:
            head = items[0]
            _emit(
                {
                    "trigger": "queued",
                    "type": "wake",
                    "payload": head,
                    "queue_depth": len(items),
                },
                suggestions=[
                    'helm-agent say "<reply>"                 # auto-acks this wake',
                    "helm-agent sleep --on-event wake        # RE-ARM after replying",
                ],
            )
            return

    _SLEEP_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SLEEP_PID_FILE.write_text(str(os.getpid()))
    try:
        asyncio.run(_sleep_async(args))
    finally:
        with contextlib.suppress(OSError):
            # Remove only if we still own it (some other sleep might've taken over).
            if _SLEEP_PID_FILE.read_text().strip() == str(os.getpid()):
                _SLEEP_PID_FILE.unlink()


async def _sleep_async(args: argparse.Namespace) -> None:
    import websockets  # local import — only sleep needs it

    deadline: float | None = None
    if args.seconds:
        deadline = asyncio.get_event_loop().time() + float(args.seconds)
    if args.until:
        end_dt = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
        deadline = asyncio.get_event_loop().time() + max(
            0.0, (end_dt - datetime.now(timezone.utc)).total_seconds()
        )

    # Parse --on-price like "AAPL.NASDAQ>250" or "TSLA.NASDAQ<200".
    price_trigger: tuple[str, str, float] | None = None
    if args.on_price:
        for op in (">=", "<=", ">", "<"):
            if op in args.on_price:
                inst, _, px = args.on_price.partition(op)
                price_trigger = (inst, op, float(px))
                break
        if price_trigger is None:
            _emit({"error": "bad --on-price", "help": "use INSTR>PRICE or INSTR<PRICE"})
            sys.exit(2)

    event_types = set((args.on_event or "").split(",")) if args.on_event else None

    stdin_task = None
    if args.on_stdin:
        async def _stdin_watch():
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, sys.stdin.readline)
        stdin_task = asyncio.create_task(_stdin_watch())

    async def _ws_loop():
        """Connect, dispatch events, reconnect on any disconnect / dial failure.

        A uvicorn restart (or any blip) drops /ws — without reconnect the sleep
        would exit with a connection error rather than the wake the caller is
        actually waiting for.
        """
        url = WS_BASE + "/ws"
        attempt = 0
        while True:
            try:
                async with websockets.connect(url) as ws:
                    attempt = 0  # successful connect resets backoff
                    while True:
                        raw = await ws.recv()
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        etype = msg.get("type")
                        payload = msg.get("payload") or {}
                        if event_types and etype in event_types:
                            return {"trigger": "event", "type": etype, "payload": payload}
                        if price_trigger and etype == "bar":
                            inst, op, target = price_trigger
                            if payload.get("instrument") == inst:
                                c = float(payload.get("close", 0))
                                hit = (op == ">" and c > target) or (op == ">=" and c >= target) \
                                      or (op == "<" and c < target) or (op == "<=" and c <= target)
                                if hit:
                                    return {"trigger": "price", "instrument": inst, "op": op,
                                            "target": target, "close": c}
            except (OSError, ConnectionError, websockets.WebSocketException):
                backoff = min(0.5 * (2 ** attempt), 10.0)
                attempt += 1
                await asyncio.sleep(backoff)

    ws_task = asyncio.create_task(_ws_loop()) if (event_types or price_trigger) else None

    tasks = [t for t in (ws_task, stdin_task) if t is not None]
    timeout = None
    if deadline is not None:
        timeout = max(0.0, deadline - asyncio.get_event_loop().time())

    if not tasks and timeout is None:
        _emit({"error": "no triggers configured",
               "help": "pass --seconds, --until, --on-price, --on-event, or --on-stdin"})
        sys.exit(2)

    if not tasks:
        await asyncio.sleep(timeout or 0)
        _emit({"trigger": "timeout", "after_seconds": args.seconds or args.until})
        return

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED,
                                            timeout=timeout)
    finally:
        for t in (ws_task, stdin_task):
            if t is not None and not t.done():
                t.cancel()

    if not done:
        _emit({"trigger": "timeout", "after_seconds": args.seconds or args.until})
        return

    result = done.pop().result()
    is_wake = isinstance(result, dict) and result.get("type") == "wake"
    suggestions = [
        'helm-agent say "<reply>"                 # auto-acks the pending wake',
        "helm-agent sleep --on-event wake        # RE-ARM after replying",
    ] if is_wake else None
    if isinstance(result, str):  # stdin readline
        _emit({"trigger": "stdin", "line": result.rstrip()})
    else:
        _emit(result, suggestions=suggestions)


# ----------------------------------------------------------------------------- #
# Session-start hook self-install (Claude Code)
# ----------------------------------------------------------------------------- #

def _install_session_hook() -> None:
    """Wire helm-agent into Claude Code's SessionStart so each session opens with state."""
    bin_path = shutil.which("helm-agent") or os.path.abspath(sys.argv[0])
    # Project-scoped hooks: per-repo .claude/settings.json
    proj = Path.cwd() / ".claude" / "settings.json"
    proj.parent.mkdir(parents=True, exist_ok=True)
    cfg: dict[str, Any] = {}
    if proj.exists():
        try:
            cfg = json.loads(proj.read_text() or "{}")
        except Exception:
            cfg = {}
    hooks = cfg.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])
    desired = {"hooks": [{"type": "command", "command": f"{bin_path} status"}]}
    # Idempotent: install only if our command isn't already present.
    if not any(
        any(h.get("command") == desired["hooks"][0]["command"] for h in (entry.get("hooks") or []))
        for entry in session_start
    ):
        session_start.append(desired)
        proj.write_text(json.dumps(cfg, indent=2) + "\n")
        _emit({"installed": str(proj), "command": desired["hooks"][0]["command"]})
    else:
        _emit({"already_installed": str(proj)})


def cmd_install(_: argparse.Namespace) -> None:
    _install_session_hook()


# ----------------------------------------------------------------------------- #
# Entry point
# ----------------------------------------------------------------------------- #

_REARM_LOOP = (
    "Re-arm loop (always end a turn parked on the wake event):\n"
    "  1. helm-agent sleep --on-event wake          # block until a webui chat msg\n"
    "  2. <process the returned trigger payload>\n"
    "  3. helm-agent say \"<reply>\"                  # post a reply (auto-acks pending)\n"
    "  4. helm-agent sleep --on-event wake          # re-arm before ending the turn\n"
    "\n"
    "  The .claude/hooks/enforce_sleep_arm.py Stop-hook BLOCKS turn-end if the\n"
    "  last Bash call wasn't `helm-agent sleep --on-event wake`. If you skip\n"
    "  step 4 the hook will re-inject a system reminder forcing you to do it.\n"
    "  `helm-agent sleep` pre-polls /api/agent/pending so any wakes that\n"
    "  arrived while you were busy are returned immediately as `trigger:\n"
    "  queued` — no message is ever dropped."
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="helm-agent", description=DESC,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=(
                                    "Examples:\n"
                                    "  helm-agent                              # live status snapshot\n"
                                    "  helm-agent bars AAPL.NASDAQ --count 50\n"
                                    "  helm-agent submit AAPL.NASDAQ BUY 10\n"
                                    "  helm-agent sleep --on-event wake        # park until next webui chat\n"
                                    "  helm-agent say \"<reply>\"                # post a reply\n"
                                    "\n"
                                    + _REARM_LOOP
                                ))
    p.add_argument("--json", action="store_true", help="emit raw JSON instead of TOON")
    p.add_argument("--version", action="version", version=f"helm-agent {VERSION}")
    sub = p.add_subparsers(dest="command")

    # Reads
    sub.add_parser("status", help="full state snapshot (default when no command)").set_defaults(fn=cmd_status)
    sub.add_parser("positions", help="open positions").set_defaults(fn=cmd_positions)
    sp = sub.add_parser("orders", help="recent orders"); sp.add_argument("--limit", type=int, default=30); sp.set_defaults(fn=cmd_orders)
    sub.add_parser("accounts", help="broker accounts (balances)").set_defaults(fn=cmd_accounts)
    sub.add_parser("portfolio", help="equity, exposure, positions snapshot").set_defaults(fn=cmd_portfolio)
    sub.add_parser("instruments", help="instruments currently loaded by the engine").set_defaults(fn=cmd_instruments)
    sp = sub.add_parser(
        "bars",
        help="OHLCV bars at any timeframe (1m..4h via Nautilus cache; 1d/1w/1M via OpenBB)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Fetch bars for an instrument at one of these timeframes:\n"
            "  1m, 5m, 15m, 30m, 1h, 4h   → aggregated from the Nautilus 1-min cache\n"
            "  1d                          → daily, via OpenBB /equity/price/historical\n"
            "  1w                          → weekly, via OpenBB\n"
            "  1M                          → monthly, via OpenBB\n"
        ),
    )
    sp.add_argument("instrument")
    sp.add_argument("--count", type=int, default=100,
                    help="how many bars to return at the chosen timeframe (default 100)")
    sp.add_argument("--timeframe", "-t", default="1m",
                    choices=list(_INTRADAY_MINUTES) + list(_DAILY_TIMEFRAMES),
                    help="bar size (default 1m)")
    sp.add_argument("--provider", default="yfinance",
                    help="OpenBB provider for daily/weekly (default yfinance)")
    sp.set_defaults(fn=cmd_bars)
    sp = sub.add_parser("decisions", help="recent AI decisions (newest first)")
    sp.add_argument("--limit", type=int, default=20); sp.set_defaults(fn=cmd_decisions)
    sub.add_parser("feeds", help="list available exotic feed sources").set_defaults(fn=cmd_feeds)
    sp = sub.add_parser("feed", help="fetch normalized FeedItems from one source")
    sp.add_argument("source"); sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--query", default=None); sp.add_argument("--param", action="append")
    sp.set_defaults(fn=cmd_feed)
    sp = sub.add_parser("oembed", help="server-side oEmbed proxy"); sp.add_argument("url"); sp.set_defaults(fn=cmd_oembed)
    sp = sub.add_parser("news", help="news headlines via OpenBB pass-through")
    sp.add_argument("--symbol", default=None); sp.add_argument("--provider", default=None)
    sp.add_argument("--limit", type=int, default=20); sp.set_defaults(fn=cmd_news)
    sp = sub.add_parser("openbb", help="arbitrary OpenBB endpoint pass-through")
    sp.add_argument("path", help='e.g. /api/v1/equity/profile')
    sp.add_argument("--param", action="append", help="key=value (repeatable)"); sp.set_defaults(fn=cmd_openbb)

    # Writes
    sp = sub.add_parser("submit", help="submit a market or limit order")
    sp.add_argument("instrument"); sp.add_argument("side", choices=["BUY", "SELL", "buy", "sell"])
    sp.add_argument("quantity", type=float); sp.add_argument("--limit", type=float, default=None,
                                                              help="limit price (omit for market)")
    sp.set_defaults(fn=cmd_submit)
    sp = sub.add_parser("cancel", help="cancel an order by id")
    sp.add_argument("order_id"); sp.set_defaults(fn=cmd_cancel)
    sp = sub.add_parser("close", help="flatten the open position on one instrument")
    sp.add_argument("instrument"); sp.set_defaults(fn=cmd_close)
    sp = sub.add_parser("close-all", help="flatten every open position")
    sp.add_argument("--exclude", default=None,
                    help="comma-separated instrument ids to keep open")
    sp.set_defaults(fn=cmd_close_all)
    sp = sub.add_parser(
        "wait-fill",
        help="block until an order reaches a terminal state",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Block on /ws until the given order id reaches FILLED, CANCELED,\n"
            "REJECTED, or EXPIRED. Useful after submit/close to chain decisions\n"
            "on the actual fill, not just the submit acknowledgement.\n"
            "\n"
            "    helm-agent submit AAPL.NASDAQ BUY 1 --json | jq -r .data.submitted.id \\\n"
            "      | xargs helm-agent wait-fill\n"
        ),
    )
    sp.add_argument("order_id")
    sp.add_argument("--timeout", type=float, default=300.0,
                    help="seconds to wait before giving up (default 300)")
    sp.set_defaults(fn=cmd_wait_fill)
    sp = sub.add_parser("add-instrument", help="add a ticker to HELM_INSTRUMENTS")
    sp.add_argument("instrument")
    sp.add_argument("--restart", action="store_true", help="re-exec uvicorn after adding")
    sp.add_argument("--no-upper", dest="upper", action="store_false", default=True,
                    help="don't upper-case the instrument id before validating")
    sp.set_defaults(fn=cmd_add_instrument)
    sp = sub.add_parser("remove-instrument", help="remove a ticker from HELM_INSTRUMENTS")
    sp.add_argument("instrument"); sp.set_defaults(fn=cmd_remove_instrument)
    sub.add_parser("restart", help="re-exec uvicorn so the engine reloads .env").set_defaults(fn=cmd_restart)
    sub.add_parser("pending", help="list queued unprocessed wake messages").set_defaults(fn=cmd_pending)
    sp = sub.add_parser(
        "decide",
        help="post an AIDecision into the Decisions tab",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Record a trading decision the agent is making (or has just made).\n"
            "The Decisions tab + GET /api/ai/decisions will pick it up via\n"
            "the broadcasted ai_decision WS event.\n"
            "\n"
            "    helm-agent decide BUY AAPL.NASDAQ --conf 0.78 \\\n"
            "        --thesis \"momentum confirmed by 30d above 200-DMA\"\n"
            "    helm-agent decide HOLD --thesis \"choppy — wait for level\"\n"
            "    helm-agent decide CLOSE GLD.ARCA --status executed \\\n"
            "        --order-id O-... --thesis \"trim concentration\"\n"
        ),
    )
    sp.add_argument("action", choices=["BUY", "SELL", "HOLD", "CLOSE",
                                       "buy", "sell", "hold", "close"])
    sp.add_argument("instrument", nargs="?", default=None)
    sp.add_argument("--thesis", required=True, help="one-line summary")
    sp.add_argument("--reasoning", default=None,
                    help='full rationale; literal, "-" for stdin, or "@/path"')
    sp.add_argument("--conf", type=float, default=0.7)
    sp.add_argument("--status", default="proposed",
                    choices=["proposed", "executed", "skipped", "rejected"])
    sp.add_argument("--order-id", default=None, dest="order_id")
    sp.set_defaults(fn=cmd_decide)

    # Nautilus artifacts (first-class)
    sub.add_parser("backtests", help="list saved Nautilus backtest results").set_defaults(fn=cmd_backtests)
    sp = sub.add_parser("backtest", help="view one backtest result (summary + trades)")
    sp.add_argument("id"); sp.add_argument("--trades", type=int, default=20,
                                            help="how many trades to include (default 20)")
    sp.set_defaults(fn=cmd_backtest)
    sub.add_parser("risk", help="list saved risk analyses").set_defaults(fn=cmd_risk)
    sp = sub.add_parser("risk-view", help="view one risk analysis (exposures + scenarios)")
    sp.add_argument("id"); sp.set_defaults(fn=cmd_risk_view)
    sub.add_parser("strategies", help="list Nautilus strategies the engine knows about").set_defaults(fn=cmd_strategies)
    sp = sub.add_parser(
        "say",
        help="post a message back to the webui chat panel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Broadcast an agent_message event so the Chat sub-tab in the AI\n"
            "Decisions widget renders it. Automatically acks the oldest pending\n"
            "wake on the server. After say, RE-ARM with:\n"
            "\n"
            "    helm-agent sleep --on-event wake\n"
            "\n"
            "MESSAGE accepts three forms — the last two sidestep shell quoting:\n"
            "  helm-agent say \"hello world\"          # literal\n"
            "  helm-agent say @/tmp/reply.md          # read from a file\n"
            "  cat reply.md | helm-agent say -        # read from stdin\n"
        ),
    )
    sp.add_argument("message", help='literal text, "@path" file, or "-" for stdin')
    sp.add_argument("--role", default="agent")
    sp.set_defaults(fn=cmd_say)
    sub.add_parser("pause", help="pause the in-engine AI trader").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="resume the in-engine AI trader").set_defaults(fn=cmd_resume)

    # Sleep
    sp = sub.add_parser(
        "sleep",
        help="block until a trigger fires (price/event/stdin/time)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Park the agent until something happens. The canonical end-of-turn\n"
            "call for an agent operating helm-agent is:\n"
            "\n"
            "    helm-agent sleep --on-event wake\n"
            "\n"
            "Before subscribing to /ws, this polls /api/agent/pending — if any\n"
            "wakes arrived while the agent was busy (or restarted), the oldest\n"
            "is returned IMMEDIATELY as `trigger: queued` and the queue depth\n"
            "is included. The matching `helm-agent say` call later acks the\n"
            "oldest entry. Pass --no-pending to skip the queue check.\n"
            "\n"
            + _REARM_LOOP
        ),
    )
    sp.add_argument("--seconds", type=float, default=None)
    sp.add_argument("--until", help="absolute ISO-8601 deadline, e.g. 2026-05-16T13:30:00Z")
    sp.add_argument("--on-price", help='INSTR>PX or INSTR<PX (e.g. "AAPL.NASDAQ>250")')
    sp.add_argument("--on-event", help='comma-separated WS event types (e.g. "bar,order,position,wake")')
    sp.add_argument("--on-stdin", action="store_true", help="wake on stdin newline")
    sp.add_argument("--no-pending", action="store_true",
                    help="skip the /api/agent/pending queue precheck, only watch live events")
    sp.add_argument("--force", action="store_true",
                    help="start a second subscriber even if one is already parked "
                         "(default: exit with already_armed status, keeps process count bounded)")
    sp.set_defaults(fn=cmd_sleep)

    sub.add_parser("install", help="self-install SessionStart hook for Claude Code").set_defaults(fn=cmd_install)
    return p


def main(argv: list[str] | None = None) -> int:
    global USE_JSON
    raw = list(sys.argv[1:] if argv is None else argv)
    # Accept --json anywhere; argparse requires top-level flags before the subcommand.
    if "--json" in raw:
        raw = [a for a in raw if a != "--json"]
        raw = ["--json", *raw]
    parser = _build_parser()
    args = parser.parse_args(raw)
    USE_JSON = bool(getattr(args, "json", False))
    fn = getattr(args, "fn", None)
    if fn is None:
        return main(["status"] + (["--json"] if USE_JSON else []))
    # Suppress noisy stack-traces — convert to structured errors on stdout.
    try:
        fn(args)
    except KeyboardInterrupt:
        _emit({"error": "interrupted"})
        return 130
    except httpx.ConnectError as e:
        _emit({"error": f"cannot reach helm backend at {DEFAULT_BASE}",
               "detail": str(e),
               "help": "is uvicorn running? set HELM_API_URL if it lives elsewhere"})
        return 1
    except Exception as e:
        _emit({"error": type(e).__name__, "detail": str(e)[:500]})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
