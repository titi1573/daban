"""
打板策略 (追涨停) - 高风险短线, 当日选股 → 次日卖出
独立项目: 与 stock-review(价值/动量策略) 完全分离

用法: python daban.py
输出: data/latest/daban.json

⚠️ 重要提示:
  1. 打板 = 追涨停, 与"低位放量不追高"的价值/动量策略是两套独立体系
  2. 高风险: 首板晋级率通常仅 30-40%, 大多数打板者亏损
  3. 无法回测: 历史涨停池免费接口拿不到, 只做当日选股, 有效性未验证
  4. 数据源: 东财涨停池(当日), 含封板资金/首次封板时间/炸板次数/连板数/换手/市值/行业

选股逻辑 (打什么板):
  - 连板数 ≤3 (打首板/二板/三板, 不追4板以上高标)
  - 炸板次数 ≤1 (最好一次封死)
  - 早盘封板 (首次封板 ≤10:30, 尾盘板=弱不碰)
  - 换手率 5~25% (过低买不进/一字板, 过高分歧大)
  - 流通市值 ≤100亿 (中小盘弹性大)
  - 封板资金 ≥5000万 (封单够厚)
  - 排除 ST
  - 排除一字板 (首封≤09:26集合竞价, 买不进)
  - 秒板标记扣分 (首封≤09:31开盘秒封, 可能买不进)
  - 20%板(创业/科创)只打首板 (连板2+断板代价大)
  - 连板2+必须主线板块 (涨停家数≥3)
  - 优选主线板块 (当日涨停家数最多的行业)

卖出规则 (次日):
  1. 次日不涨停 → 卖 (不板就走)
  2. 次日继续涨停 → 持有 (吃连板)
  3. 次日低开 >5% → 无条件止损
"""
import json
import sys
from datetime import date
from pathlib import Path

import akshare as ak

LATEST_DIR = Path("data/latest")

# 选股参数
MAX_CONSECUTIVE = 3      # 最多打3板(不追高标)
MIN_TURNOVER = 5         # 换手率下限 %
MAX_TURNOVER = 25        # 换手率上限 %
MAX_FLOAT_MV = 100e8     # 流通市值上限 100亿
MAX_BREAK = 1            # 炸板次数上限
EARLY_SEAL = "103000"    # 首次封板时间不晚于 10:30
MIN_SEAL_AMOUNT = 5000e4  # 封板资金下限 5000万
# 复盘后新增(2026-09-21): 排除买不进/高波动/孤立高标
EXCLUDE_YIZIBAN = True             # 排除一字板(首封≤YIZIBAN_SEAL)
YIZIBAN_SEAL = "092600"            # 一字板: 首封≤09:26(集合竞价)
MIAOBAN_SEAL = "093100"            # 秒板: 首封≤09:31(开盘秒封, 可能买不进)
EXCLUDE_20PCT_MULTI = True         # 20%板(创业/科创)只打首板, 连板2+排除
MAINLINE_MIN_COUNT = 3             # 主线板块最少涨停家数(连板股须达到)
REQUIRE_MAINLINE_FOR_MULTI = True  # 连板2+必须主线板块
BREAK_RATE_GOOD = 40               # 情绪"好"的炸板率上限 %
BREAK_RATE_MID = 60                # 情绪"中"的炸板率上限 %


def is_st(name):
    return "ST" in str(name).upper()


def board_pct(code, name=""):
    """近似涨跌停幅度 (主板10/创业科创20/北交30/ST 5)"""
    c = str(code)
    if "ST" in str(name).upper():
        return 0.05
    if c.startswith(("688", "689", "300", "301")):
        return 0.20
    if c.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def latest_trade_date():
    """最近一个交易日(≤今天), 周末/节假日回落到上一交易日, 失败兜底为今天"""
    today = date.today()
    try:
        df = ak.tool_trade_date_hist_sina()
        days = []
        for d in df["trade_date"]:
            d = d.date() if hasattr(d, "date") else d  # datetime/Timestamp → date
            if isinstance(d, date) and d <= today:
                days.append(d)
        if days:
            return max(days)
    except Exception:
        pass
    return today


def fetch_limit_up(trade_date):
    """最近交易日的涨停池"""
    df = ak.stock_zt_pool_em(date=trade_date.strftime("%Y%m%d"))
    return df


def screen(df, trade_date):
    total = len(df)
    if total == 0:
        return {"date": trade_date.strftime("%Y-%m-%d"), "total_limit_up": 0,
                "sentiment": "无数据", "picks": []}

    df = df.copy()
    df["连板数"] = df["连板数"].astype(int)
    df["炸板次数"] = df["炸板次数"].astype(int)
    df["换手率"] = df["换手率"].astype(float)
    df["流通市值"] = df["流通市值"].astype(float)
    df["封板资金"] = df["封板资金"].astype(float)

    max_conn = int(df["连板数"].max())
    total_break = int(df["炸板次数"].sum())
    break_homes = int((df["炸板次数"] > 0).sum())  # 炸板家数(至少炸过一次)
    sector_cnt = df["所属行业"].value_counts().to_dict()
    top_sectors = sorted(sector_cnt.items(), key=lambda x: -x[1])[:5]

    picks = []
    for _, r in df.iterrows():
        name = r["名称"]
        if is_st(name):
            continue
        conn = int(r["连板数"])
        if conn > MAX_CONSECUTIVE:
            continue
        turnover = float(r["换手率"])
        if turnover < MIN_TURNOVER or turnover > MAX_TURNOVER:
            continue
        float_mv = float(r["流通市值"])
        if float_mv > MAX_FLOAT_MV:
            continue
        breaks = int(r["炸板次数"])
        if breaks > MAX_BREAK:
            continue
        seal_time = str(r["首次封板时间"])
        if seal_time > EARLY_SEAL:
            continue
        seal_amt = float(r["封板资金"])
        if seal_amt < MIN_SEAL_AMOUNT:
            continue
        sector = r["所属行业"]
        code = str(r["代码"])

        # 复盘后新增过滤(2026-09-21): 排除买不进/高波动/孤立高标
        # 1) 一字板(集合竞价封板, 开盘即涨停买不进)
        if EXCLUDE_YIZIBAN and seal_time <= YIZIBAN_SEAL:
            continue
        miaoban = seal_time <= MIAOBAN_SEAL  # 秒板(开盘秒封), 可能买不进
        # 2) 20%板(创业/科创)只打首板, 连板2+断板代价大
        if EXCLUDE_20PCT_MULTI and conn >= 2 and board_pct(code, name) >= 0.20:
            continue
        # 3) 连板2+必须主线板块(涨停家数≥N), 避免孤立高标
        if REQUIRE_MAINLINE_FOR_MULTI and conn >= 2 and sector_cnt.get(sector, 0) < MAINLINE_MIN_COUNT:
            continue

        # 评分: 封板质量 + 连板 + 主线板块加成 + 秒板扣分
        score = 0
        score += 0 if breaks == 0 else -20
        score += 10 if conn >= 2 else 0
        score += sector_cnt.get(sector, 0) * 3
        score += -5 if miaoban else 0
        picks.append({
            "code": r["代码"], "name": name, "sector": sector,
            "consecutive": conn, "price": round(float(r["最新价"]), 2),
            "turnover": round(turnover, 1), "break_times": breaks,
            "seal_amount_yi": round(seal_amt / 1e8, 2),
            "first_seal_time": seal_time,
            "float_mv_yi": round(float_mv / 1e8, 1),
            "miaoban": miaoban,
            "score": score,
        })

    picks.sort(key=lambda x: (-x["score"], -x["consecutive"]))

    # 情绪周期: 涨停家数 + 连板高度 + 炸板率(封板质量)
    break_rate = break_homes / total * 100 if total else 0
    if total >= 60 and max_conn >= 4 and break_rate <= BREAK_RATE_GOOD:
        sentiment = "好(可打)"
    elif total >= 30 and break_rate <= BREAK_RATE_MID:
        sentiment = "中(谨慎打)"
    else:
        sentiment = "差(少打/不打)"

    return {
        "date": trade_date.strftime("%Y-%m-%d"),
        "total_limit_up": total,
        "max_consecutive": max_conn,
        "total_break_times": total_break,
        "break_homes": break_homes,
        "break_rate": round(break_rate, 1),
        "sentiment": sentiment,
        "top_sectors": [{"sector": s, "count": c} for s, c in top_sectors],
        "picks": picks,
    }


def main():
    global EXCLUDE_YIZIBAN, EXCLUDE_20PCT_MULTI, REQUIRE_MAINLINE_FOR_MULTI
    if "--no-yiziban" in sys.argv:
        EXCLUDE_YIZIBAN = False
    if "--no-20pct" in sys.argv:
        EXCLUDE_20PCT_MULTI = False
    if "--no-mainline" in sys.argv:
        REQUIRE_MAINLINE_FOR_MULTI = False

    print("=== 打板选股(追涨停) ===\n")
    trade_date = latest_trade_date()
    print(f"交易日: {trade_date.strftime('%Y-%m-%d')}\n")
    try:
        df = fetch_limit_up(trade_date)
    except Exception as e:
        print(f"涨停池获取失败: {e}")
        return
    result = screen(df, trade_date)
    print(f"当日涨停 {result['total_limit_up']} 家 | 最高 {result['max_consecutive']} 连板 | "
          f"炸板率 {result['break_rate']}% | 情绪: {result['sentiment']}")
    print(f"主线板块: {result['top_sectors']}\n")
    print(f"打板候选 {len(result['picks'])} 只:")
    print(f"{'名称':8s}{'代码':8s}{'行业':8s}{'连板':>4}{'现价':>8}{'换手':>6}{'炸板':>4}{'封单亿':>7}{'市值亿':>7}{'首封':>7}{'评分':>6}")
    for p in result["picks"]:
        nm = ("秒" + p["name"]) if p.get("miaoban") else p["name"]
        print(f"{nm:8s}{p['code']:8s}{p['sector']:8s}{p['consecutive']:>4}{p['price']:>8.2f}"
              f"{p['turnover']:>6.1f}{p['break_times']:>4}{p['seal_amount_yi']:>7.2f}{p['float_mv_yi']:>7.1f}"
              f"{p['first_seal_time']:>7}{p['score']:>6}")

    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    (LATEST_DIR / "daban.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果 → {LATEST_DIR / 'daban.json'}")


if __name__ == "__main__":
    main()
