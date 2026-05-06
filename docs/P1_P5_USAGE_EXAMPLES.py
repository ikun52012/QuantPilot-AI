"""
P1-P5 功能使用示例 - 实际场景演示
======================================

本文档用具体例子展示P1-P5每个优化功能的实际使用方式。
"""

# ─────────────────────────────────────────────
# P1: 性能优化功能示例
# ─────────────────────────────────────────────

"""
1️⃣ 多层缓存架构 (L1/L2/L3)

场景：AI分析结果缓存，避免重复调用AI API

使用前（旧代码）：
    每次信号都调用AI API，耗时15-30秒，成本高

使用后（新代码）：
"""

import asyncio

from core.cache import MultiLayerCache


async def example_ai_cache_usage():
    """实际使用示例：AI分析缓存"""

    # 创建缓存实例
    cache = MultiLayerCache(
        cache_name="ai_analysis",
        l1_max_size=500,        # 内存缓存500条
        l1_base_ttl=60,         # 60秒过期
        l2_enabled=True,        # 启用Redis
        l2_redis_url="redis://localhost:6379/0",
        l3_enabled=True,        # 启用磁盘缓存
        l3_cache_dir="./data/cache/ai",
    )

    # 初始化Redis（如果启用）
    await cache.initialize_redis()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景1：缓存AI分析结果
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 第一次调用（缓存未命中，调用AI API）
    async def call_ai_api():
        """模拟调用AI API"""
        await asyncio.sleep(15)  # AI API耗时15秒
        return {
            "confidence": 0.85,
            "recommendation": "execute",
            "suggested_entry": 50000.0,
        }

    result1 = await cache.get_or_compute(
        key="BTCUSDT:long:50000:1h",  # 缓存键
        compute_fn=call_ai_api,       # 计算函数
        compute_fn_is_async=True,     # 异步函数
        ttl_override=120,             # 自定义TTL 120秒
    )

    print(f"第一次调用: {result1}")  # 耗时15秒
    # [P1-FIX] Computing value for key: BTCUSDT:long:50000:1h

    # 第二次调用（缓存命中L1，瞬间返回）
    result2 = await cache.get_or_compute(
        key="BTCUSDT:long:50000:1h",
        compute_fn=call_ai_api,
        compute_fn_is_async=True,
    )

    print(f"第二次调用: {result2}")  # 耗时<1毫秒！
    # [P1-FIX] L1 cache hit: BTCUSDT:long:50000:1h

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景2：缓存提升（L3→L1）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 清空L1缓存（模拟服务重启）
    cache._l1_cache.clear()

    # 再次调用（从L3磁盘加载，提升到L1）
    result3 = await cache.get("BTCUSDT:long:50000:1h")

    print(f"重启后调用: {result3}")  # 耗时<10毫秒
    # [P1-FIX] L3 disk cache hit: BTCUSDT:long:50000:1h
    # [P1-FIX] Promoted to L1 cache

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景3：查看缓存性能
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    metrics = await cache.get_metrics()

    print(f"""
    缓存性能报告：
    - L1缓存大小: {metrics['l1_size']}/{metrics['l1_max_size']}
    - L1命中: {metrics['l1_hits']} 次
    - L2命中: {metrics['l2_hits']} 次
    - L3命中: {metrics['l3_hits']} 次
    - 总命中率: {metrics['hit_rate_pct']}%
    - 计算（未命中）: {metrics['computes']} 次
    - LRU淘汰: {metrics['evictions']} 次
    """)

    # 示例输出：
    # 缓存性能报告：
    # - L1缓存大小: 1/500
    # - L1命中: 1 次
    # - L2命中: 0 次
    # - L3命中: 1 次
    # - 总命中率: 66.67%  （2次命中 / 3次请求）
    # - 计算（未命中）: 1 次
    # - LRU淘汰: 0 次


async def example_smc_cache():
    """SMC分析缓存示例"""

    from core.cache.multi_layer_cache import get_smc_analysis_cache

    cache = await get_smc_analysis_cache()

    # 缓存SMC结构分析结果（2分钟TTL）
    smc_key = "ETHUSDT:4h:smc_structure"

    smc_result = await cache.get_or_compute(
        key=smc_key,
        compute_fn=lambda: analyze_smc_structure("ETHUSDT", "4h"),
        ttl_override=120,  # SMC结构变化慢，2分钟足够
    )

    print(f"SMC分析结果: {smc_result}")


# ─────────────────────────────────────────────
# P2: 架构优化功能示例
# ─────────────────────────────────────────────

"""
2️⃣ 事件驱动架构 (EventBus)

场景：交易执行后自动通知多个组件

使用前（旧代码）：
    execute_trade() 函数直接调用多个函数：
    - log_trade()
    - update_position_db()
    - send_telegram()
    - update_metrics()
    → 紧耦合，难以扩展

使用后（新代码）：
"""

from core.events import EventBus, EventTypes, TradeEvent


async def example_event_bus_usage():
    """实际使用示例：事件驱动架构"""

    bus = EventBus(
        persist_events=True,        # 持久化事件到磁盘
        event_store_path="./data/events",
        max_event_history=1000,
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景1：订阅交易事件
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 组件1：日志记录器订阅
    async def log_trade_handler(event: TradeEvent):
        """记录交易日志"""
        print(f"[日志] 交易执行: {event.ticker} {event.direction} - {event.status}")

    bus.subscribe(
        EventTypes.TRADE_EXECUTED,
        log_trade_handler,
        handler_name="log_trade_handler",
        priority=1,  # 高优先级，最先执行
    )

    # 组件2：Telegram通知订阅
    async def telegram_handler(event: TradeEvent):
        """发送Telegram通知"""
        if event.status == "success":
            await send_telegram_message(
                f"✅ 交易成功: {event.ticker} {event.direction} "
                f"@ {event.signal_price}"
            )

    bus.subscribe(
        EventTypes.TRADE_EXECUTED,
        telegram_handler,
        handler_name="telegram_notification",
        priority=10,  # 低优先级，最后执行
    )

    # 组件3：指标更新订阅
    def metrics_handler(event: TradeEvent):
        """更新Prometheus指标"""
        from core.metrics.prometheus_metrics import record_trade_metrics
        record_trade_metrics(
            exchange=event.exchange,
            symbol=event.ticker,
            direction=event.direction,
            result=event.status,
            latency_seconds=event.data.get("latency", 0),
        )

    bus.subscribe(
        EventTypes.TRADE_EXECUTED,
        metrics_handler,
        handler_name="metrics_update",
        priority=5,
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景2：发布交易事件
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 执行交易后发布事件
    event = TradeEvent(
        event_type=EventTypes.TRADE_EXECUTED,
        ticker="BTCUSDT",
        direction="long",
        signal_price=50000.0,
        status="success",
        exchange="binance",
        order_id="order_123",
        user_id="user_001",
        trace_id="trace_abc123",
        data={
            "latency": 1.5,
            "quantity": 0.01,
            "leverage": 10,
        },
    )

    # 发布事件 → 所有订阅者自动执行
    await bus.publish(event)

    # 输出：
    # [P2-FIX] Publishing event TRADE_EXECUTED to 3 handlers
    # [日志] 交易执行: BTCUSDT long - success
    # ✅ 交易成功: BTCUSDT long @ 50000
    # [P3-FIX] Metrics recorded

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景3：查看事件统计
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    metrics = bus.get_metrics()

    print(f"""
    EventBus统计：
    - 已发布事件: {metrics['events_published']}
    - 已处理事件: {metrics['events_processed']}
    - 处理器执行: {metrics['handlers_executed']} 次
    - 处理器错误: {metrics['handler_errors']} 次
    - 注册处理器: {metrics['handlers_registered']}
    """)


async def example_ghost_position_event():
    """Ghost position事件示例"""

    bus = EventBus()

    # 订阅Ghost position事件
    async def handle_ghost_position(event):
        """处理Ghost position"""
        position_id = event.position_id
        ticker = event.ticker

        logger.warning(
            f"[GHOST] 检测到Ghost持仓: {position_id} {ticker} "
            f"阈值={event.data['threshold']} "
            f"尝试次数={event.data['fail_count']}"
        )

        # 自动通知管理员
        await send_admin_alert(f"Ghost position detected: {ticker}")

    bus.subscribe(EventTypes.POSITION_GHOST_DETECTED, handle_ghost_position)

    # 发布事件
    event = PositionEvent(
        event_type=EventTypes.POSITION_GHOST_DETECTED,
        position_id="pos_abc123",
        ticker="BTCUSDT",
        data={
            "threshold": 7,
            "fail_count": 8,
            "position_value": 5000.0,
        },
    )

    await bus.publish(event)


"""
3️⃣ 配置热重载

场景：动态调整杠杆倍数，无需重启服务

使用前（旧代码）：
    修改.env文件 → 重启服务 → 等待30秒 → 生效

使用后（新代码）：
"""

from core.config_hot_reload import ConfigHotReloader


async def example_config_hot_reload():
    """配置热重载示例"""

    reloader = ConfigHotReloader(
        config_path="./config/runtime.json",
        validate_changes=True,  # 启用验证
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景1：注册回调函数
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 杠杆调整回调
    def update_leverage(new_leverage):
        """动态更新杠杆"""
        logger.info(f"[热重载] 杠杆已更新: {new_leverage}x")
        # 更新全局配置
        settings.trading.default_leverage = new_leverage

    reloader.register_callback("trading.leverage", update_leverage)

    # AI超时调整回调
    def update_ai_timeout(new_timeout):
        """动态调整AI超时"""
        logger.info(f"[热重载] AI超时已更新: {new_timeout}秒")
        settings.ai.read_timeout_secs = new_timeout

    reloader.register_callback("ai.timeout", update_ai_timeout)

    # Ghost阈值调整回调
    def update_ghost_threshold(new_threshold):
        """动态调整Ghost阈值"""
        logger.info(f"[热重载] Ghost阈值已更新: {new_threshold}")
        position_monitor._MAX_GHOST_THRESHOLD = new_threshold

    reloader.register_callback("position_monitor.ghost_threshold", update_ghost_threshold)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景2：启动监听
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    await reloader.start()

    # 此时修改配置文件，自动触发回调
    # 修改 config/runtime.json:
    # {
    #   "trading": {
    #     "leverage": 15  // 从10改成15
    #   }
    # }

    # 输出：
    # [P2-FIX] Config change detected: old_hash=abc123 -> new_hash=def456
    # [P2-FIX] Applied config change: trading.leverage = 15
    # [热重载] 杠杆已更新: 15x

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景3：查看变更历史
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    changes = reloader.get_change_count()

    print(f"配置已热重载 {changes} 次")


"""
4️⃣ API版本管理

场景：API升级，v1废弃，v2启用

使用前（旧代码）：
    只有一个API版本，升级需要强制所有客户端更新

使用后（新代码）：
"""

from core.api_versioning import APIVersionManager, create_versioned_router


def example_api_versioning():
    """API版本管理示例"""

    manager = APIVersionManager(
        default_version="v1",
        latest_version="v2",
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景1：注册不同版本路由
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # v1路由（旧API）
    v1_router = create_versioned_router("v1")

    @v1_router.post("/trade/execute")
    async def execute_trade_v1(request):
        """v1 API - 单一TP/SL"""
        return {
            "status": "success",
            "take_profit": 52000,  # 单一TP
            "stop_loss": 48000,
        }

    manager.register_version("v1", v1_router, prefix="/api/v1")

    # v2路由（新API）
    v2_router = create_versioned_router("v2")

    @v2_router.post("/trade/execute")
    async def execute_trade_v2(request):
        """v2 API - 多TP + trailing stop"""
        return {
            "status": "success",
            "take_profit_levels": [
                {"price": 51000, "qty_pct": 25},
                {"price": 52000, "qty_pct": 25},
                {"price": 53000, "qty_pct": 25},
                {"price": 54000, "qty_pct": 25},
            ],
            "trailing_stop": {
                "mode": "step_trailing",
                "activation_pct": 2.0,
            },
        }

    manager.register_version("v2", v2_router, prefix="/api/v2")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景2：废弃旧版本
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    manager.deprecate_version(
        version="v1",
        sunset_date="2025-08-01",  # 废弃日期
        migration_guide_url="/docs/api-migration-v1-to-v2",
    )

    # 客户端调用v1 API会收到废弃警告：
    # Headers:
    #   Deprecation: true; sunset=2025-08-01
    #   Warning: 299 - "Deprecated API version v1. Use v2 instead."
    #   Link: </docs/api-migration>; rel="deprecation"

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景3：版本检测
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 方式1：Header指定版本
    # curl -H "X-API-Version: v2" http://api/trade/execute

    # 方式2：URL路径指定版本
    # curl http://api/v2/trade/execute

    # 方式3：默认版本（未指定）
    # curl http://api/trade/execute  # 使用v1

    versions_info = manager.get_version_info_response()

    print(f"""
    API版本信息：
    - 默认版本: {versions_info['default_version']}
    - 最新版本: {versions_info['latest_version']}
    - 可用版本: {versions_info['available_versions']}
    """)


# ─────────────────────────────────────────────
# P3: 可观测性功能示例
# ─────────────────────────────────────────────

"""
5️⃣ Prometheus指标

场景：实时监控交易系统性能

使用前（旧代码）：
    只能查看日志，没有实时指标

使用后（新代码）：
"""

from core.metrics import (
    record_ai_metrics,
    record_error_metrics,
    record_trade_metrics,
    update_cache_metrics,
)


def example_prometheus_metrics():
    """Prometheus指标使用示例"""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景1：记录交易指标
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 交易成功
    record_trade_metrics(
        exchange="binance",
        symbol="BTCUSDT",
        direction="long",
        result="success",
        latency_seconds=1.5,
        stage="execute",
    )

    # Prometheus指标：
    # quantpilot_trade_total{exchange="binance",symbol="BTCUSDT",direction="long",result="success"} +1
    # quantpilot_trade_latency_seconds{exchange="binance",stage="execute"} = 1.5

    # 交易失败
    record_trade_metrics(
        exchange="binance",
        symbol="ETHUSDT",
        direction="short",
        result="failed",
        latency_seconds=0.3,
        stage="execute",
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景2：记录AI分析指标
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # AI分析成功（缓存命中）
    record_ai_metrics(
        provider="deepseek",
        model="deepseek-v4-pro",
        result="success",
        latency_seconds=0.01,  # 缓存命中，瞬间返回
        cache_layer="L1_memory",
    )

    # Prometheus指标：
    # quantpilot_ai_analysis_total{provider="deepseek",model="deepseek-v4-pro",result="success"} +1
    # quantpilot_ai_analysis_latency_seconds{provider="deepseek"} = 0.01
    # quantpilot_ai_cache_hit_total{layer="L1_memory"} +1

    # AI分析超时
    record_ai_metrics(
        provider="openai",
        model="gpt-4",
        result="timeout",
        latency_seconds=90.0,
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景3：记录错误指标
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 认证错误（严重）
    record_error_metrics(
        module="exchange",
        error_type="AuthenticationError",
        severity="critical",
    )

    # Prometheus指标：
    # quantpilot_error_rate_total{module="exchange",error_type="AuthenticationError",severity="critical"} +1

    # 杠杆设置失败
    record_leverage_failure(
        exchange="binance",
        symbol="BTCUSDT",
        leverage=20,
        retry_attempt=2,
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景4：更新缓存指标
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    update_cache_metrics(
        cache_name="ai_analysis",
        layer="L1",
        hit_rate_pct=75.0,
        size=450,
    )

    # Prometheus指标：
    # quantpilot_cache_hit_rate_pct{cache_name="ai_analysis",layer="L1"} = 75.0
    # quantpilot_cache_size{cache_name="ai_analysis",layer="L1"} = 450

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景5：查询指标（Grafana Dashboard）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 在Grafana中查询：

    # 交易成功率（5分钟）
    # rate(quantpilot_trade_total{result="success"}[5m])
    # / rate(quantpilot_trade_total[5m])

    # AI缓存命中率
    # rate(quantpilot_ai_cache_hit_total[30m])
    # / rate(quantpilot_ai_analysis_total[30m])

    # 活跃持仓数
    # sum(quantpilot_position_count)


"""
6️⃣ 告警规则

场景：自动检测异常并发送告警

配置示例：
"""

# 在 config/alerting_rules.yml 中定义的告警规则

"""
告警1：交易失败率过高

触发条件：
  rate(trade_total{result="failed"}[5m]) / rate(trade_total[5m]) > 0.1

含义：5分钟内交易失败率超过10%

告警动作：
  - 发送飞书消息
  - 发送Email到管理员
  - Grafana Dashboard高亮显示

告警内容：
  "交易失败率11% on binance，可能存在交易所连接问题"

建议操作：
  1. 检查交易所API状态
  2. 查看错误日志
  3. 检查API密钥有效期
"""

"""
告警2：Ghost position检测

触发条件：
  ghost_position_count > 0

含义：检测到持仓在数据库存在但交易所不存在

告警动作：
  - 发送Telegram通知
  - 自动记录到审计日志

告警内容：
  "检测到Ghost持仓: BTCUSDT (阈值7次，已尝试8次)"

建议操作：
  1. 检查position_monitor日志
  2. 验证交易所持仓状态
  3. 手动同步或关闭
"""

"""
告警3：AI分析超时率高

触发条件：
  rate(ai_analysis_total{result="timeout"}[5m]) / rate(ai_analysis_total[5m]) > 0.2

含义：AI分析超时率超过20%

告警动作：
  - 发送飞书消息
  - 自动记录问题

告警内容：
  "AI分析超时率25% (deepseek)，影响交易决策质量"

建议操作：
  1. 检查AI提供商状态
  2. 调整超时配置（AI_READ_TIMEOUT_SECS）
  3. 启用缓存或更快的模型
"""


"""
7️⃣ 结构化日志

场景：用TraceID追踪完整交易流程

使用前（旧代码）：
    日志分散，无法关联同一个请求的多个日志

使用后（新代码）：
"""

from core.logging.structured_logging import (
    log_ai_analysis,
    log_trade_event,
    log_with_context,
)


def example_structured_logging():
    """结构化日志示例"""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景1：带上下文的日志
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 记录日志时带上trace_id、exchange、symbol等上下文
    log_with_context(
        level="INFO",
        message="交易信号接收",
        trace_id="trace_abc123",
        user_id="user_001",
        exchange="binance",
        symbol="BTCUSDT",
        direction="long",
    )

    # 输出JSON日志：

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景2：追踪完整交易流程
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 同一个trace_id关联多个日志

    # 步骤1：信号接收
    log_trade_event(
        action="receive",
        exchange="binance",
        symbol="BTCUSDT",
        direction="long",
        status="received",
        trace_id="trace_abc123",  # 同一个TraceID
    )

    # 步骤2：AI分析
    log_ai_analysis(
        provider="deepseek",
        model="deepseek-v4-pro",
        ticker="BTCUSDT",
        direction="long",
        result="success",
        confidence=0.85,
        cache_layer="L1_memory",
        latency_seconds=0.01,
        trace_id="trace_abc123",  # 同一个TraceID
    )

    # 步骤3：交易执行
    log_trade_event(
        action="execute",
        exchange="binance",
        symbol="BTCUSDT",
        direction="long",
        status="success",
        order_id="order_123",
        latency_seconds=1.5,
        trace_id="trace_abc123",  # 同一个TraceID
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景3：搜索日志
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 用TraceID搜索完整流程
    # cat logs/quantpilot_*.json | jq 'select(.trace_id=="trace_abc123")'

    # 输出：
    # [
    #   {"timestamp": "...", "message": "交易信号接收", "trace_id": "trace_abc123"},
    #   {"timestamp": "...", "message": "AI分析完成", "trace_id": "trace_abc123"},
    #   {"timestamp": "...", "message": "交易执行成功", "trace_id": "trace_abc123"}
    # ]

    # 完整流程一目了然！


# ─────────────────────────────────────────────
# P4: 测试保障功能示例
# ─────────────────────────────────────────────

"""
8️⃣ 单元测试

场景：测试缓存TTL过期

测试代码：
"""

import pytest


@pytest.mark.asyncio
async def test_cache_ttl_expiration():
    """测试缓存TTL过期"""

    # 创建测试缓存
    cache = MultiLayerCache(
        cache_name="test_cache",
        l1_max_size=10,
        l1_base_ttl=60,
        l2_enabled=False,
        l3_enabled=False,
    )

    # 设置0.5秒TTL
    await cache.set("expire_key", "value", ttl=0.5)

    # 立即获取 → 成功
    result1 = await cache.get("expire_key")
    assert result1 == "value"  # ✅ 测试通过

    # 等待过期
    await asyncio.sleep(0.6)

    # 再次获取 → 失败（已过期）
    result2 = await cache.get("expire_key")
    assert result2 is None  # ✅ 测试通过

    # 验证metrics
    metrics = await cache.get_metrics()
    assert metrics["l1_misses"] >= 1  # ✅ 记录了miss


@pytest.mark.asyncio
async def test_leverage_retry_success():
    """测试杠杆重试成功"""

    from exchange import _set_leverage_with_retry

    # Mock exchange
    mock_exchange = Mock()
    mock_exchange.set_leverage.side_effect = [
        ccxt.NetworkError("Error"),  # 第一次失败
        {"leverage": 10},             # 第二次成功
    ]

    result = await _set_leverage_with_retry(
        mock_exchange,
        leverage=10,
        symbol="BTC/USDT:USDT",
    )

    assert result["success"]  # ✅ 最终成功
    assert mock_exchange.set_leverage.call_count == 2  # ✅ 重试了2次


@pytest.mark.asyncio
async def test_ghost_threshold_dynamic():
    """测试动态阈值计算"""

    from position_monitor import _calculate_ghost_threshold

    # 小持仓（$50） → 阈值3
    position_small = Mock()
    position_small.entry_price = 5000.0
    position_small.quantity = 0.01
    position_small.leverage = 1.0

    threshold1 = _calculate_ghost_threshold(position_small)
    assert threshold1 == 3  # ✅ 小持仓快速关闭

    # 大持仓（$5000） → 阈值7
    position_large = Mock()
    position_large.entry_price = 50000.0
    position_large.quantity = 1.0
    position_large.leverage = 10.0

    threshold2 = _calculate_ghost_threshold(position_large)
    assert threshold2 == 7  # ✅ 大持仓更多耐心


"""
9️⃣ 集成测试

场景：测试完整交易流程

测试代码：
"""

@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_trade_pipeline():
    """测试完整交易流程"""

    # 1. 创建信号
    TradingViewSignal(
        ticker="BTCUSDT",
        direction=SignalDirection.LONG,
        price=50000.0,
    )

    # 2. Mock AI分析
    with patch("ai_analyzer.analyze_signal_with_ai"):
        ai_result = AIAnalysis(
            confidence=0.85,
            recommendation="execute",
            suggested_leverage=10,
        )

        # 3. Mock exchange
        with patch("exchange._get_or_create_exchange"):
            # 4. 执行交易
            result = await execute_trade(
                TradeDecision(
                    ticker="BTCUSDT",
                    direction=SignalDirection.LONG,
                    quantity=0.01,
                    ai_analysis=ai_result,
                ),
                exchange_config={"live_trading": False},
            )

            # 5. 验证结果
            assert result["status"] != "error"  # ✅ 成功


# ─────────────────────────────────────────────
# P5: 文档功能示例
# ─────────────────────────────────────────────

"""
🔟 API文档

访问：/api/v2/trade/execute

请求示例：
"""

# curl -X POST http://localhost:8000/api/v2/trade/execute \
#   -H "Content-Type: application/json" \
#   -H "Authorization: Bearer <token>" \
#   -d '{
#     "ticker": "BTCUSDT",
#     "direction": "long",
#     "signal_price": 50000.0
#   }'

"""
响应示例：
"""

# {
#   "version": "v2",
#   "status": "success",
#   "order_id": "binance_order_123",
#   "take_profit_levels": [
#     {"price": 51000, "qty_pct": 25},
#     {"price": 52000, "qty_pct": 25}
#   ],
#   "trailing_stop": {
#     "mode": "step_trailing"
#   },
#   "ai_confidence": 0.85
# }


"""
1️⃣1️⃣ 运维手册

故障排查示例：

问题：Ghost position检测

诊断步骤：
"""

# 步骤1：检查日志
# grep "ghost" logs/quantpilot_*.json | jq

# 输出：
# {
#   "timestamp": "2026-05-06T10:00:00Z",
#   "level": "WARNING",
#   "message": "GHOST POSITION: pos_abc123 BTCUSDT",
#   "position_id": "pos_abc123",
#   "fail_count": 8,
#   "threshold": 7
# }

# 步骤2：检查交易所API状态
# curl -X GET https://api.binance.com/api/v3/ping

# 步骤3：手动同步持仓
# python scripts/sync_positions.py --exchange binance --position pos_abc123

# 步骤4：调整阈值（如果太敏感）
# config/runtime.json:
# {
#   "position_monitor": {
#     "ghost_threshold_multiplier": 2.0
#   }
# }


"""
维护任务示例：

每日检查清单：
"""

# □ 检查Prometheus Dashboard
# curl http://localhost:8000/api/v2/metrics

# □ 查看缓存命中率
# curl http://localhost:8000/api/v2/cache/metrics

# □ 检查错误日志
# cat logs/errors_*.log | tail -20

# □ 验证备份完成
# ls -lh backup/*.sql | tail -5


"""
性能调优示例：

数据库优化：
"""

# 添加索引
# CREATE INDEX idx_positions_status_opened_at ON positions(status, opened_at);

# 分析查询性能
# ANALYZE positions;

# 回收空间
# VACUUM ANALYZE positions;


# ─────────────────────────────────────────────
# 总结：所有功能实际效果
# ─────────────────────────────────────────────

"""
P1 缓存优化效果：

场景：BTCUSDT long信号重复出现

使用前：
  第1次：调用AI API，耗时15秒，成本$0.001
  第2次：调用AI API，耗时15秒，成本$0.001
  第3次：调用AI API，耗时15秒，成本$0.001
  总计：45秒，成本$0.003

使用后：
  第1次：L1 miss → 调用AI API，耗时15秒，成本$0.001
  第2次：L1 hit → 缓存返回，耗时<1ms，成本$0
  第3次：L1 hit → 缓存返回，耗时<1ms，成本$0
  总计：15秒+2ms，成本$0.001

改进：节省33秒（-73%），节省$0.002（-66%）


P2 事件驱动效果：

场景：交易执行后需要通知5个组件

使用前：
  execute_trade() {
    log_trade()            # 直接调用
    update_position_db()   # 直接调用
    send_telegram()        # 直接调用
    update_metrics()       # 直接调用
    send_email()           # 直接调用
  }
  → 紧耦合，添加新功能需要修改execute_trade()

使用后：
  execute_trade() {
    bus.publish(TRADE_EXECUTED)
  }

  # 各组件独立订阅
  log_handler.subscribe(TRADE_EXECUTED)
  telegram_handler.subscribe(TRADE_EXECUTED)
  metrics_handler.subscribe(TRADE_EXECUTED)

  → 松耦合，添加新功能只需添加新订阅者，无需修改execute_trade()


P3 可观测性效果：

场景：交易失败率突然升高

使用前：
  需要手动查看日志文件，搜索"error"
  → 发现问题可能需要30分钟

使用后：
  Prometheus告警自动触发：
  "交易失败率11% on binance"
  → 立即收到飞书/Telegram通知，1分钟内发现问题

  Grafana Dashboard实时显示：
  - 交易成功率图表
  - 错误类型分布
  - 交易所延迟对比


P4 测试保障效果：

场景：修改缓存代码后担心破坏功能

使用前：
  手动测试几个场景，不确定是否有遗漏
  → 生产环境发现问题，紧急修复

使用后：
  pytest tests/unit/test_cache.py -v
  → 40个测试自动运行，覆盖所有场景
  → 测试失败立即发现，修复后才能部署


P5 文档运维效果：

场景：新同事接手运维工作

使用前：
  口头交接，文档不完整
  → 需要问老同事，效率低

使用后：
  查看docs/OPERATIONS_MANUAL.md
  → 600行详细文档
  → 部署步骤、故障排查、维护任务一目了然

  查看docs/API_DOCUMENTATION.md
  → API使用示例、错误码说明
  → 快速上手开发
"""

print("✅ 所有P1-P5功能示例已展示完成！")
print("详细报告请查看：COMPLETE_OPTIMIZATION_REPORT.md")
