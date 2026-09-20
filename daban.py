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
  - 优选主线板块 (当日涨停家数最多的行业)

卖出规则 (次日):
  1. 次日不涨停 → 卖 (不板就走)
  2. 次日继续涨停 → 持有 (吃连板)
  3. 次日低开 >5% → 无条件止损
"""
import json
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


def is_st(name):
    return "ST" in str(name).upper()


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
        # 评分: 封板质量 + 连板 + 主线板块加成
        score = 0
        score += 0 if breaks == 0 else -20
        score += 10 if conn >= 2 else 0
        score += sector_cnt.get(sector, 0) * 3
        picks.append({
            "code": r["代码"], "name": name, "sector": sector,
            "consecutive": conn, "price": round(float(r["最新价"]), 2),
            "turnover": round(turnover, 1), "break_times": breaks,
            "seal_amount_yi": round(seal_amt / 1e8, 2),
            "first_seal_time": seal_time,
            "float_mv_yi": round(float_mv / 1e8, 1),
            "score": score,
        })

    picks.sort(key=lambda x: (-x["score"], -x["consecutive"]))

    # 情绪周期
    if total >= 60 and max_conn >= 4:
        sentiment = "好(可打)"
    elif total >= 30:
        sentiment = "中(谨慎打)"
    else:
        sentiment = "差(少打/不打)"

    return {
        "date": trade_date.strftime("%Y-%m-%d"),
        "total_limit_up": total,
        "max_consecutive": max_conn,
        "total_break_times": total_break,
        "break_homes": break_homes,
        "break_rate": round(break_homes / total * 100, 1) if total else 0,
        "sentiment": sentiment,
        "top_sectors": [{"sector": s, "count": c} for s, c in top_sectors],
        "picks": picks,
    }


def main():
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
        print(f"{p['name']:8s}{p['code']:8s}{p['sector']:8s}{p['consecutive']:>4}{p['price']:>8.2f}"
              f"{p['turnover']:>6.1f}{p['break_times']:>4}{p['seal_amount_yi']:>7.2f}{p['float_mv_yi']:>7.1f}"
              f"{p['first_seal_time']:>7}{p['score']:>6}")

    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    (LATEST_DIR / "daban.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果 → {LATEST_DIR / 'daban.json'}")


if __name__ == "__main__":
    main()
