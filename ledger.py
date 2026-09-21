"""
打板账户台账 - 记录每笔打板的买入/卖出与实际盈亏, 维护账户资金
独立项目: 与 stock-review 完全分离

用法:
  python ledger.py            # 查看账户台账(现金/持仓/已平仓/总资产/胜率) + 生成 data/LEDGER.md
  python ledger.py --buy      # 读 data/latest/daban.json 候选, 挂涨停价买入(建仓)
  python ledger.py --reset    # 重置账户(清空所有记录)

闭环: daban.py(选股) → ledger.py --buy(买入) → sell_sim.py(次日卖出) → ledger.py(汇总)
"""
import json
import sys
from datetime import date
from pathlib import Path

import akshare as ak

DATA_DIR = Path("data")
LATEST_DIR = DATA_DIR / "latest"
STATE_FILE = DATA_DIR / "ledger.json"
LEDGER_MD = DATA_DIR / "LEDGER.md"
DABAN_JSON = LATEST_DIR / "daban.json"

# ---- 账户参数 ----
CAPITAL = 10000         # 起始资金 1万 (可改)
MAX_POSITIONS = 5       # 最多持仓数 (README: 分散3~5只)
POSITION_PCT = 0.20     # 单票仓位 20% (README: 单票10~20%)

# ---- 成本模型 (A股, 与 stock-review 一致) ----
COMMISSION_RATE = 0.00025   # 佣金 万2.5
COMMISSION_MIN = 5.0        # 最低佣金 5元
STAMP_TAX_RATE = 0.0005     # 印花税 卖出 0.05%


def raw_code(code):
    return str(code).split(".")[0] if "." in str(code) else str(code)


def limit_pct(code, name=""):
    """近似涨跌停幅度 (主板10/创业科创20/北交30/ST 5)"""
    c = raw_code(code).lower().replace("sh", "").replace("sz", "").replace("bj", "")
    if "ST" in str(name).upper():
        return 0.05
    if c.startswith(("688", "689", "300", "301")):
        return 0.20
    if c.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def trade_cost(amount, is_sell):
    """单边成本: 佣金(最低5元) + 卖出印花税"""
    c = max(COMMISSION_MIN, amount * COMMISSION_RATE)
    if is_sell:
        c += amount * STAMP_TAX_RATE
    return c


def today_str():
    return date.today().strftime("%Y%m%d")


def fetch_daily(code, start_date, end_date=None):
    """不复权日K线 (打板用真实价格), 东财优先失败降级新浪, 失败返回 None"""
    end = end_date or today_str()
    c = raw_code(code)
    try:
        df = ak.stock_zh_a_hist(symbol=c, period="daily",
                                start_date=start_date, end_date=end, adjust="")
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    try:
        sym = ("sh" if c.startswith(("6", "9")) else "sz") + c
        df = ak.stock_zh_a_daily(symbol=sym, start_date=start_date, end_date=end, adjust="")
        if df is not None and not df.empty:
            return df.rename(columns={"date": "日期", "open": "开盘", "high": "最高",
                                      "low": "最低", "close": "收盘"})
    except Exception:
        pass
    return None


# ---- 台账状态 ----
def init_state():
    return {
        "initial_capital": CAPITAL,
        "cash": float(CAPITAL),
        "start_date": date.today().strftime("%Y-%m-%d"),
        "last_buy_date": None,
        "positions": [],
        "closed_trades": [],
        "log": [],
    }


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return init_state()


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state["cash"] = round(state["cash"], 2)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_daban_result():
    if DABAN_JSON.exists():
        with open(DABAN_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ---- 买入 ----
def affordable_shares(price, cash):
    """给定价格与现金, 返回买得起的最大整手股数 (100股一手)"""
    if price <= 0:
        return 0
    shares = int(cash / price // 100) * 100
    while shares > 0:
        cost = shares * price
        if cost + trade_cost(cost, False) <= cash + 1e-6:
            return shares
        shares -= 100
    return 0


def buy_from_picks(state, result):
    """从 daban.json 候选建仓 (挂涨停价买入); 返回本次买入数量"""
    trade_date = result.get("date")
    if not trade_date:
        return 0
    if state.get("last_buy_date") == trade_date:
        print(f"  {trade_date} 已建仓, 跳过重复买入")
        return 0

    sentiment = result.get("sentiment", "")
    picks = result.get("picks", [])
    if "差" in sentiment or not picks:
        print(f"  情绪「{sentiment}」, 不打板 (空仓)")
        state["last_buy_date"] = trade_date
        save_state(state)
        return 0

    n_target = MAX_POSITIONS if "好" in sentiment else min(MAX_POSITIONS, 3)
    held = {p["code"] for p in state["positions"]}
    bought = 0
    for p in picks[:n_target]:
        code, name = str(p["code"]), p["name"]
        price = float(p.get("price", 0) or 0)
        if code in held or price <= 0:
            continue
        shares = int(CAPITAL * POSITION_PCT / price // 100) * 100
        if shares <= 0:
            shares = affordable_shares(price, state["cash"])
            if shares <= 0:
                continue
        cost = shares * price
        fee = trade_cost(cost, False)
        if cost + fee > state["cash"]:
            shares = affordable_shares(price, state["cash"])
            if shares <= 0:
                continue
            cost = shares * price
            fee = trade_cost(cost, False)
        state["cash"] -= cost + fee
        state["positions"].append({
            "code": code, "name": name,
            "entry_date": trade_date, "entry_price": round(price, 2),
            "shares": shares, "cost_per_share": round((cost + fee) / shares, 4),
            "last_price": round(price, 2),
        })
        state["log"].append({"date": trade_date, "type": "buy", "code": code, "name": name,
                             "msg": f"{price:.2f}×{shares}股 (成本含佣 ¥{cost + fee:.2f})"})
        held.add(code)
        bought += 1
    state["last_buy_date"] = trade_date
    save_state(state)
    return bought


# ---- 卖出 ----
def sell_position(state, pos, exit_date, exit_price, reason):
    """平仓并记录每笔打板的实际盈亏 (现金回笼 + 已平仓记录)"""
    shares = pos["shares"]
    proceeds = shares * exit_price
    fee = trade_cost(proceeds, True)
    cost = shares * pos["cost_per_share"]
    net = proceeds - fee - cost
    state["cash"] += proceeds - fee
    state["closed_trades"].append({
        "code": pos["code"], "name": pos["name"],
        "entry_date": pos["entry_date"], "entry_price": pos["entry_price"],
        "exit_date": exit_date, "exit_price": round(exit_price, 2),
        "shares": shares,
        "net_pnl": round(net, 2),
        "net_return_pct": round(net / cost * 100, 2) if cost else 0,
        "exit_reason": reason,
    })
    state["log"].append({"date": exit_date, "type": "sell", "code": pos["code"], "name": pos["name"],
                         "msg": f"{reason}: {exit_price:.2f}×{shares}股 (净 ¥{net:+.2f})"})


# ---- 汇总 ----
def account_summary(state):
    market_value = sum(p["shares"] * p.get("last_price", p["entry_price"]) for p in state["positions"])
    equity = state["cash"] + market_value
    realized = sum(t["net_pnl"] for t in state["closed_trades"])
    unrealized = sum((p.get("last_price", p["entry_price"]) - p["entry_price"]) * p["shares"]
                     for p in state["positions"])
    closed = state["closed_trades"]
    wins = sum(1 for t in closed if t["net_pnl"] > 0)
    return {
        "cash": round(state["cash"], 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "return_pct": round((equity - state["initial_capital"]) / state["initial_capital"] * 100, 2),
        "closed_count": len(closed),
        "win_rate": round(wins / len(closed) * 100, 1) if closed else None,
        "open_count": len(state["positions"]),
    }


def print_account(state):
    s = account_summary(state)
    print("\n=== 打板账户台账 ===")
    print(f"起始资金 ¥{state['initial_capital']:,.0f} | 现金 ¥{s['cash']:,.2f} | "
          f"市值 ¥{s['market_value']:,.2f} | 总资产 ¥{s['equity']:,.2f}")
    wr = f"胜率 {s['win_rate']}%" if s["win_rate"] is not None else "尚未平仓"
    print(f"收益率 {s['return_pct']:+.2f}% | 已实现 {s['realized_pnl']:+,.2f} | "
          f"浮动 {s['unrealized_pnl']:+,.2f} | 已平仓 {s['closed_count']} 笔({wr}) | 持仓 {s['open_count']} 只")

    if state["positions"]:
        print("\n—— 当前持仓 ——")
        print(f"{'名称':<10}{'代码':<8}{'买入日':<12}{'买入价':>8}{'股数':>8}{'现价':>8}{'浮动盈亏':>12}")
        for p in state["positions"]:
            last = p.get("last_price", p["entry_price"])
            pnl = (last - p["entry_price"]) * p["shares"]
            print(f"{p['name']:<10}{p['code']:<8}{p['entry_date']:<12}{p['entry_price']:>8.2f}"
                  f"{p['shares']:>8}{last:>8.2f}{pnl:>+12.2f}")

    if state["closed_trades"]:
        print("\n—— 已平仓(每笔打板盈亏) ——")
        print(f"{'名称':<10}{'代码':<8}{'买入日':<12}{'卖出日':<12}{'买入价':>8}{'卖出价':>8}{'股数':>7}{'净盈亏':>10}{'收益率':>9}  原因")
        for t in state["closed_trades"]:
            print(f"{t['name']:<10}{t['code']:<8}{t['entry_date']:<12}{t['exit_date']:<12}"
                  f"{t['entry_price']:>8.2f}{t['exit_price']:>8.2f}{t['shares']:>7}"
                  f"{t['net_pnl']:>+10.2f}{t['net_return_pct']:>+9.2f}%  {t['exit_reason']}")


def render_ledger_md(state):
    s = account_summary(state)
    L = ["# 打板账户台账\n",
         f"> 起始资金 ¥{state['initial_capital']:,} | 费用: 佣金万2.5(最低¥5)、卖出印花税0.05%\n",
         "---\n",
         "## 一、账户总览\n",
         "| 现金 | 持仓市值 | 总资产 | 已实现盈亏 | 浮动盈亏 | 收益率 | 胜率 |",
         "|---|---|---|---|---|---|---|"]
    wr = f"{s['win_rate']}%" if s["win_rate"] is not None else "--"
    L.append(f"| ¥{s['cash']:,.2f} | ¥{s['market_value']:,.2f} | ¥{s['equity']:,.2f} | "
             f"{s['realized_pnl']:+,.2f} | {s['unrealized_pnl']:+,.2f} | {s['return_pct']:+.2f}% | {wr} |")
    L.append("")
    if state["positions"]:
        L.append("## 二、当前持仓\n")
        L.append("| 标的 | 买入日 | 买入价 | 股数 | 现价 | 浮动盈亏 |")
        L.append("|---|---|---|---|---|---|")
        for p in state["positions"]:
            last = p.get("last_price", p["entry_price"])
            pnl = (last - p["entry_price"]) * p["shares"]
            L.append(f"| {p['name']} {p['code']} | {p['entry_date']} | {p['entry_price']:.2f} | "
                     f"{p['shares']} | {last:.2f} | {pnl:+,.2f} |")
        L.append("")
    if state["closed_trades"]:
        L.append("## 三、已平仓记录(每笔打板盈亏)\n")
        L.append("| 卖出日 | 标的 | 买入日 | 买入价 | 卖出价 | 股数 | 净盈亏 | 收益率 | 原因 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for t in state["closed_trades"]:
            L.append(f"| {t['exit_date']} | {t['name']} {t['code']} | {t['entry_date']} | {t['entry_price']:.2f} | "
                     f"{t['exit_price']:.2f} | {t['shares']} | **{t['net_pnl']:+,.2f}** | {t['net_return_pct']:+.2f}% | {t['exit_reason']} |")
        L.append("")
    L.append("## 四、决策规则(持续遵循)\n")
    L.append("1. 打板只打: 连板≤3、炸板≤1、早盘封板(≤10:30)、换手5~25%、流通市值≤100亿、封板资金≥5000万, 排除ST。")
    L.append("2. 次日不涨停→卖(不板就走); 继续涨停→持有(最多5日); 低开>5%→无条件止损。")
    L.append("3. 单票20%、分散3~5只; 情绪差(涨停<30家或炸板率高)少打/不打。")
    L.append("4. 每笔买入/卖出都记台账, 记录实际盈亏。")
    L.append("")
    L.append("---")
    L.append("> ⚠️ 打板是低胜率高赔率的高风险游戏, 首板晋级率通常仅30~40%, 本台账只记录模拟结果, 不构成投资建议。")
    L.append("")
    LEDGER_MD.write_text("\n".join(L), encoding="utf-8")


def main():
    if "--reset" in sys.argv:
        save_state(init_state())
        print("账户已重置")
        return

    state = load_state()

    if "--buy" in sys.argv:
        result = load_daban_result()
        if not result or not result.get("picks"):
            print("缺少 daban.json 或暂无候选, 请先运行 python daban.py")
            return
        n = buy_from_picks(state, result)
        print(f"本次建仓 {n} 只 (候选 {len(result['picks'])} 只, 情绪「{result.get('sentiment')}」)")

    print_account(state)
    render_ledger_md(state)
    print(f"\n台账 → {LEDGER_MD}")


if __name__ == "__main__":
    main()
