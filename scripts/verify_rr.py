"""Verify R:R configurations for all timeframes."""
import sys

sys.path.insert(0, '.')

from timeframe_exits import get_timeframe_config, validate_multi_tp_rr

print("=== Standard 25:25:25:25 Distribution ===")
for tf in ['1m', '5m', '15m', '1h', '4h', '1D']:
    config = get_timeframe_config(tf)
    result = validate_multi_tp_rr(config)
    print(f"{tf}: worst={result['worst_case_rr']:.2f}:1, single={result['single_tp_min_rr']:.2f}:1, pass={result['meets_minimum']}")

print("\n=== Conservative 50:30:20:0 Distribution ===")
for tf in ['15m', '1h', '1D']:
    config = get_timeframe_config(tf)
    result = validate_multi_tp_rr(config, (50.0, 30.0, 20.0, 0.0))
    print(f"{tf}: worst={result['worst_case_rr']:.2f}:1, pass={result['meets_minimum']}")

print("\n=== Single TP 100:0:0:0 Distribution ===")
for tf in ['15m', '1h', '1D']:
    config = get_timeframe_config(tf)
    result = validate_multi_tp_rr(config, (100.0, 0.0, 0.0, 0.0))
    print(f"{tf}: single_tp={result['worst_case_rr']:.2f}:1, pass={result['meets_minimum']}")
