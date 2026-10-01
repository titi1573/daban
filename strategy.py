"""
策略预设: 保守 / 激进 (选股打分 + 仓位参数, 供 daban.py 与 ledger.py 共享)

用法:
  python daban.py --conservative / --aggressive        # 选股
  python ledger.py --buy --conservative / --aggressive # 建仓(自动应用对应仓位)
"""
DEFAULT_STRATEGY = "aggressive"

STRATEGY_PRESETS = {
    "conservative": {
        "label": "保守",
        # 选股打分
        "conn_score": {1: 10, 2: 0, 3: -10},   # 首板优先
        "exclude_miaoban": True,                # 秒板直接排除
        "break_good": 30, "break_mid": 45,      # 情绪更严
        "consecutive_desc": False,              # 排序: 首板优先
        # 仓位
        "position_pct": 0.10,                   # 单票 10%
        "max_positions": 3,                     # 最多 3 只
    },
    "aggressive": {
        "label": "激进",
        # 选股打分
        "conn_score": {1: 0, 2: 10, 3: 5},      # 精修连板
        "exclude_miaoban": False,               # 秒板只扣分
        "break_good": 40, "break_mid": 60,      # 情绪更宽
        "consecutive_desc": True,               # 排序: 连板优先
        # 仓位
        "position_pct": 0.20,                   # 单票 20%
        "max_positions": 5,                     # 最多 5 只
    },
}


def resolve_strategy(argv):
    """从命令行参数解析策略名 (默认 aggressive)"""
    return "conservative" if "--conservative" in argv else DEFAULT_STRATEGY
