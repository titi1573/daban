"""
次日卖出模拟 - 按卖出三规则模拟打板的次日及后续卖出, 更新台账
用法: python sell_sim.py

卖出规则 (与 README 一致):
  1. 低开 >5% → 无条件止损 (开盘价卖)
  2. 继续涨停 → 持有 (吃连板溢价, 最多连续持有 5 日)
  3. 不涨停 → 卖 (不板就走, 收盘价卖)

说明/局限:
  - 打板在 T 日涨停价买入, 本脚本模拟 T+1 及之后的卖出 (自然满足 A 股 T+1)
  - "不板就走"用收盘价成交近似, 无法模拟盘中破板 (免费接口无分钟数据)
  - 停牌日无K线会自动跳过; 数据未出(下一交易日未到)则保持持有
"""
from ledger import load_state, save_state, limit_pct, fetch_daily, today_str, sell_position

MAX_HOLD_DAYS = 5  # 连续涨停最多持有5日, 之后强制止盈


def _f(x):
    try:
        v = float(x)
        return v if v == v else None  # NaN → None
    except (TypeError, ValueError):
        return None


def decide(bars, entry_price, lp, max_hold=MAX_HOLD_DAYS):
    """纯决策函数 (可单测): bars 为次日及之后的 [{'date','open','close'}] 列表"""
    prev_close = entry_price
    hold_days = 0
    for bar in bars:
        if bar["open"] <= prev_close * 0.95:
            return {"status": "sell", "date": bar["date"], "price": round(bar["open"], 2),
                    "reason": "低开>5%止损"}
        if bar["close"] >= prev_close * (1 + lp - 0.005):
            hold_days += 1
            if hold_days >= max_hold:
                return {"status": "sell", "date": bar["date"], "price": round(bar["close"], 2),
                        "reason": f"连续涨停{hold_days}日强制止盈"}
            prev_close = bar["close"]
            continue
        return {"status": "sell", "date": bar["date"], "price": round(bar["close"], 2),
                "reason": "不板就走"}
    last = round(bars[-1]["close"], 2) if bars else round(entry_price, 2)
    return {"status": "holding", "last_price": last, "msg": f"仍涨停持有({len(bars)}日)"}


def simulate_sell(code, name, entry_date, entry_price):
    """拉取次日K线并决策"""
    df = fetch_daily(code, entry_date, today_str())
    if df is None:
        return {"status": "error", "msg": "无K线数据"}
    bars = []
    for _, r in df.iterrows():
        d = str(r["日期"])[:10]
        if d <= entry_date:
            continue
        o, c = _f(r["开盘"]), _f(r["收盘"])
        if o is None or c is None:
            continue
        bars.append({"date": d, "open": o, "close": c})
    if not bars:
        return {"status": "waiting", "msg": "次日数据未出(下一交易日未到)"}
    return decide(bars, entry_price, limit_pct(code, name))


def main():
    state = load_state()
    positions = state.get("positions", [])
    if not positions:
        print("无持仓, 无需卖出")
        return

    print(f"=== 次日卖出模拟 ({len(positions)} 只持仓) ===\n")
    still_open = []
    sold = 0
    for pos in positions:
        tag = f"{pos['name']}({pos['code']})"
        res = simulate_sell(pos["code"], pos["name"], pos["entry_date"], pos["entry_price"])
        if res["status"] == "sell":
            sell_position(state, pos, res["date"], res["price"], res["reason"])
            ret = (res["price"] - pos["entry_price"]) / pos["entry_price"] * 100
            print(f"  [卖出] {tag} {pos['entry_date']}→{res['date']} "
                  f"{pos['entry_price']:.2f}→{res['price']:.2f} ({ret:+.1f}%) {res['reason']}")
            sold += 1
        elif res["status"] == "holding":
            pos["last_price"] = res.get("last_price", pos["last_price"])
            still_open.append(pos)
            print(f"  [持有] {tag} {res.get('msg', '')}")
        else:
            still_open.append(pos)
            print(f"  [{('等待' if res['status'] == 'waiting' else '跳过')}] {tag} {res.get('msg', '')}")

    state["positions"] = still_open
    save_state(state)
    print(f"\n本次卖出 {sold} 只 | 剩余持仓 {len(still_open)} 只")


if __name__ == "__main__":
    main()
