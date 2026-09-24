"""
打板策略历史回测 (用 Tushare 重建涨停池)

数据源: daily(日线全市场) + daily_basic(换手/流通市值) + stock_basic(行业/名称)
局限: 无 limit_list_d 权限 → 拿不到封单金额/首次封板时间/炸板次数,
      因此无法回测"一字板/秒板/封单/炸板"过滤, 只回测核心选股逻辑。

用法: python backtest.py [--days N]
输出: 总体统计 + 按连板数分组 + 按板块主线分组 (首板优先 vs 连板奖励 对照)
"""
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import tushare as ts

CACHE_DIR = Path("data/backtest_cache")

# 策略参数 (与 daban.py 一致)
MAX_CONSECUTIVE = 3
MIN_TURNOVER = 5
MAX_TURNOVER = 25
MAX_FLOAT_MV_WAN = 1_000_000   # 流通市值上限 100亿元 = 100万(万元)
MAINLINE_MIN_COUNT = 3         # 主线板块最少涨停家数
MAX_HOLD_DAYS = 5
LOW_OPEN_STOP_PCT = 3          # 低开>3%止损
N_PICKS = 3                    # 每日买入前N (情绪"中"对应3只)


def load_token():
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("TUSHARE_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


pro = ts.pro_api(load_token())


def limit_pct(code, name=""):
    if "ST" in str(name).upper():
        return 0.05
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def is_limit_up(r):
    code = r["ts_code"].split(".")[0]
    return r["pct_chg"] >= (limit_pct(code, r["name"]) * 100 - 0.5)


def get_cached(name, fetcher):
    """缓存到 CSV, 避免重复拉取"""
    f = CACHE_DIR / name
    if f.exists():
        return pd.read_csv(f, dtype={"ts_code": str, "trade_date": str})
    df = fetcher()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(f, index=False)
    return df


def fetch_trade_days(n):
    """最近 n + MAX_HOLD_DAYS 个交易日 (升序)"""
    start = "20250101"  # 固定起点, 覆盖所有回测窗口 (避免缓存范围不足)
    end = date.today().strftime("%Y%m%d")

    def f():
        return pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
    cal = get_cached("trade_cal.csv", f)
    days = sorted(cal[cal["is_open"] == 1]["cal_date"].astype(str).tolist())
    return days[-(n + MAX_HOLD_DAYS):]


def fetch_day(d):
    """拉某日全市场 daily + daily_basic"""
    def fd():
        df = pro.daily(trade_date=d)
        time.sleep(0.3)
        return df
    daily = get_cached(f"daily_{d}.csv", fd)

    def fb():
        df = pro.daily_basic(trade_date=d, fields="ts_code,turnover_rate,circ_mv")
        time.sleep(0.3)
        return df
    basic = get_cached(f"basic_{d}.csv", fb)
    return daily, basic


def fetch_info():
    def f():
        return pro.stock_basic(list_status="L", fields="ts_code,name,industry")
    return get_cached("stock_basic.csv", f)


def build_pool(days, daily_map, basic_map, info):
    """重建每日涨停池 (含连板数); 返回 {date: [row,...]} 与 {date: set(涨停代码)}"""
    limit_sets = {}
    pools = {}
    for d in days:
        daily, basic = daily_map[d], basic_map[d]
        m = daily.merge(basic[["ts_code", "turnover_rate", "circ_mv"]], on="ts_code", how="left") \
                 .merge(info, on="ts_code", how="left")
        m["_code"] = m["ts_code"].str.split(".").str[0]
        m = m[m["_code"].str.startswith(("00", "30", "60", "68"))]  # 只留沪深主板/创业/科创
        m = m[~m["name"].fillna("").str.contains("ST", case=False)]  # 排除 ST
        m["_limit"] = m.apply(is_limit_up, axis=1)
        lim = m[m["_limit"]].copy()
        # 过滤异常(新股首日暴涨等): 涨跌幅不应超过对应板幅+0.5
        lim = lim[lim.apply(lambda r: r["pct_chg"] <= limit_pct(r["_code"], r["name"]) * 100 + 0.5, axis=1)]
        limit_sets[d] = set(lim["ts_code"])
        pools[d] = lim
    # 计算连板数
    for i, d in enumerate(days):
        rows = []
        for _, r in pools[d].iterrows():
            code = r["ts_code"]
            conn = 1
            j = i - 1
            while j >= 0 and code in limit_sets[days[j]]:
                conn += 1
                j -= 1
            lp = limit_pct(code, r["name"])
            yiziban = r["open"] >= r["pre_close"] * (1 + lp - 0.005)
            rows.append({"ts_code": code, "name": r["name"], "industry": r["industry"],
                         "close": r["close"], "pct_chg": r["pct_chg"],
                         "turnover": r["turnover_rate"], "circ_mv": r["circ_mv"],
                         "conn": conn, "yiziban": yiziban})
        pools[d] = pd.DataFrame(rows)
    return pools


def score_pick(p, sector_cnt, mode):
    """打分: mode='first'(首板优先) 或 'chase'(连板奖励,旧版)"""
    conn = p["conn"]
    if mode == "first":
        conn_score = 10 if conn == 1 else (0 if conn == 2 else -10)
    else:  # chase
        conn_score = 10 if conn >= 2 else 0
    return conn_score + sector_cnt.get(p["industry"], 0) * 3


def apply_filters(pool):
    """核心选股过滤 (含排除一字板/买不进)"""
    pool = pool[~pool["yiziban"]]  # 排除一字板(开盘即涨停, 买不进)
    pool = pool[(pool["conn"] <= MAX_CONSECUTIVE)]
    pool = pool[(pool["turnover"] >= MIN_TURNOVER) & (pool["turnover"] <= MAX_TURNOVER)]
    pool = pool[(pool["circ_mv"] <= MAX_FLOAT_MV_WAN)]
    pool = pool.dropna(subset=["industry"])
    return pool


def simulate_sell(entry, code, name, next_bars):
    """按卖出三规则模拟; 返回 (exit_price, exit_date, reason) 或 (None,None,'持有中')"""
    lp = limit_pct(code, name)
    prev = entry
    for bar in next_bars:
        o, c, d = bar["open"], bar["close"], bar["date"]
        if o <= prev * (1 - LOW_OPEN_STOP_PCT / 100):
            return o, d, "低开止损"
        if c >= prev * (1 + lp - 0.005):
            prev = c
            continue
        return c, d, "不板就走"
    return None, None, "持有中"


def run_backtest(n_days):
    days = fetch_trade_days(n_days)
    buy_days = days[:n_days]
    print(f"回测区间: {buy_days[0]} ~ {buy_days[-1]} ({n_days} 个买入日, 共 {len(days)} 天数据)")

    info = fetch_info()
    daily_map, basic_map = {}, {}
    print("拉取日线/指标数据...")
    for i, d in enumerate(days):
        daily_map[d], basic_map[d] = fetch_day(d)
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(days)}")
    pools = build_pool(days, daily_map, basic_map, info)
    print("数据就绪\n")

    # 次日OHLC映射 (用于卖出)
    def next_bars_of(code, idx):
        bars = []
        for k in range(idx + 1, min(idx + 1 + MAX_HOLD_DAYS, len(days))):
            d = days[k]
            row = daily_map[d]
            hit = row[row["ts_code"] == code]
            if len(hit):
                bars.append({"date": d, "open": hit.iloc[0]["open"], "close": hit.iloc[0]["close"]})
            else:
                break  # 停牌, 顺延不了就停
        return bars

    for mode in ("first", "chase"):
        trades = []
        for i, d in enumerate(buy_days):
            idx = days.index(d)
            pool = apply_filters(pools[d])
            if pool.empty:
                continue
            sector_cnt = pool["industry"].value_counts().to_dict()
            pool = pool.copy()
            pool["score"] = pool.apply(lambda r: score_pick(r, sector_cnt, mode), axis=1)
            pool = pool.sort_values("score", ascending=False).head(N_PICKS)
            for _, p in pool.iterrows():
                code = p["ts_code"].split(".")[0]
                bars = next_bars_of(p["ts_code"], idx)
                if not bars:
                    continue
                exit_px, exit_d, reason = simulate_sell(p["close"], code, p["name"], bars)
                if exit_px is None:
                    continue
                ret = (exit_px / p["close"] - 1) * 100
                trades.append({
                    "date": d, "code": code, "name": p["name"], "industry": p["industry"],
                    "conn": p["conn"], "ret": ret, "reason": reason,
                    "mainline": sector_cnt.get(p["industry"], 0) >= MAINLINE_MIN_COUNT,
                })
        report(mode, trades)


def report(mode, trades):
    if not trades:
        print(f"[{mode}] 无交易")
        return
    df = pd.DataFrame(trades)
    label = "首板优先" if mode == "first" else "连板奖励(旧)"
    def stats(sub):
        if sub.empty:
            return {"n": 0, "win": None, "avg": None, "sum": None}
        return {
            "n": len(sub),
            "win": round((sub["ret"] > 0).mean() * 100, 1),
            "avg": round(sub["ret"].mean(), 2),
            "sum": round(sub["ret"].sum(), 2),
        }
    print(f"\n===== 策略: {label} =====")
    s = stats(df)
    print(f"总计: {s['n']} 笔 | 胜率 {s['win']}% | 平均 {s['avg']}% | 累计 {s['sum']}%")
    print("按连板数:")
    for conn in (1, 2, 3):
        st = stats(df[df["conn"] == conn])
        tag = "首板" if conn == 1 else ("二板" if conn == 2 else "三板")
        print(f"  {tag}(连板{conn}): {st['n']} 笔 | 胜率 {st['win']}% | 平均 {st['avg']}% | 累计 {st['sum']}%")
    print("按板块:")
    for mainline in (True, False):
        st = stats(df[df["mainline"] == mainline])
        tag = "主线(≥3家)" if mainline else "非主线(<3家)"
        print(f"  {tag}: {st['n']} 笔 | 胜率 {st['win']}% | 平均 {st['avg']}% | 累计 {st['sum']}%")


if __name__ == "__main__":
    n = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 60
    run_backtest(n)
