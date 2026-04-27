"""
Internationalization Router - Multi-language support.
Provides translation management for multiple languages.
"""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from loguru import logger

from core.auth import get_current_user


router = APIRouter(prefix="/api/i18n", tags=["Internationalization"])


_SUPPORTED_LANGUAGES = {
    "en": "English",
    "zh": "中文",
    "ja": "日本語",
    "ko": "한국어",
    "es": "Español",
}


_TRANSLATIONS = {
    "en": {
        "nav": {
            "home": "Home",
            "dashboard": "Dashboard",
            "my_trading": "My Trading",
            "positions": "Positions",
            "history": "History",
            "analytics": "Analytics",
            "charts": "Charts",
            "social": "Signals",
            "backtest": "Backtest",
            "strategies": "Strategies",
            "strategy_editor": "Editor",
            "subscription": "Subscription",
            "settings": "Settings",
            "admin": "Admin",
        },
        "common": {
            "ticker": "Ticker",
            "timeframe": "Timeframe",
            "days": "Days",
            "price": "Price",
            "direction": "Direction",
            "confidence": "Confidence",
            "reason": "Reason",
            "name": "Name",
            "time": "Time",
            "actions": "Actions",
            "status": "Status",
            "yes": "Yes",
            "no": "No",
            "active": "active",
            "draft": "draft",
        },
        "actions": {
            "load": "Load",
            "share": "Share",
            "save": "Save",
            "reset": "Reset",
            "use": "Use",
            "edit": "Edit",
            "activate": "Activate",
            "deactivate": "Deactivate",
            "delete": "Delete",
            "subscribe": "Subscribe",
            "unsubscribe": "Unsubscribe",
            "follow": "Follow",
        },
        "pages": {
            "charts": {
                "kicker": "Market Workspace",
                "description": "Live market context, historical OHLCV, indicators, open positions, and executed signal markers.",
                "market_chart": "Market Chart",
                "change_24h": "24h Change",
                "volume_24h": "Volume 24h",
                "position_markers": "Position Markers",
                "signal_markers": "Signal Markers",
                "no_positions": "No open position markers",
                "no_signals": "No executed signal markers",
                "marker": "Marker",
            },
            "social": {
                "kicker": "Community Desk",
                "description": "Share trade ideas, follow providers, subscribe to signals, and review community activity.",
                "shared_signals": "Shared Signals",
                "subscriptions": "Subscriptions",
                "active_providers": "Active Providers",
                "top_ticker": "Top Ticker",
                "share_signal": "Share Signal",
                "leaderboard": "Leaderboard",
                "signal_feed": "Signal Feed",
                "my_subscriptions": "My Signal Subscriptions",
                "provider": "Provider",
                "no_signals": "No shared signals yet",
                "no_subscriptions": "No signal subscriptions",
                "no_leaderboard": "No leaderboard data",
                "signal": "Signal",
                "auto_execute": "Auto Execute",
                "max_position": "Max Position",
                "subscribed": "Subscribed",
            },
            "editor": {
                "kicker": "Strategy Builder",
                "description": "Create reusable custom strategies from templates, conditions, risk settings, and exit rules.",
                "templates": "Templates",
                "strategy_draft": "Strategy Draft",
                "entry_json": "Entry Conditions JSON",
                "exit_json": "Exit Conditions JSON",
                "risk_json": "Risk JSON",
                "tp_json": "TP Levels JSON",
                "trailing_json": "Trailing Stop JSON",
                "saved_strategies": "Saved Strategies",
                "no_templates": "No templates available",
                "no_saved_strategies": "No saved custom strategies",
            },
        },
        "kpi": {
            "total_trades": "Total Trades",
            "win_rate": "Win Rate",
            "total_pnl": "Total PnL",
            "open_positions": "Open Positions",
            "max_drawdown": "Max Drawdown",
            "sharpe_ratio": "Sharpe Ratio",
        },
        "trading": {
            "buy": "Buy",
            "sell": "Sell",
            "long": "Long",
            "short": "Short",
            "entry": "Entry",
            "exit": "Exit",
            "stop_loss": "Stop Loss",
            "take_profit": "Take Profit",
            "trailing_stop": "Trailing Stop",
            "pnl": "PnL",
            "fees": "Fees",
        },
        "backtest": {
            "run_backtest": "Run Backtest",
            "strategy": "Strategy",
            "timeframe": "Timeframe",
            "initial_capital": "Initial Capital",
            "position_size": "Position Size",
            "winning_trades": "Winning Trades",
            "losing_trades": "Losing Trades",
            "profit_factor": "Profit Factor",
            "max_dd": "Max Drawdown",
        },
        "dca": {
            "create_dca": "Create DCA",
            "max_entries": "Max Entries",
            "entry_spacing": "Entry Spacing",
            "sizing_method": "Sizing Method",
            "average_down": "Average Down",
            "average_up": "Average Up",
        },
        "grid": {
            "create_grid": "Create Grid",
            "grid_count": "Grid Count",
            "grid_spacing": "Grid Spacing",
            "neutral": "Neutral",
            "long_bias": "Long Bias",
            "short_bias": "Short Bias",
        },
        "websocket": {
            "connected": "WebSocket Connected",
            "disconnected": "WebSocket Disconnected",
            "position_update": "Position Update",
            "price_update": "Price Update",
        },
        "messages": {
            "success": "Success",
            "error": "Error",
            "warning": "Warning",
            "info": "Info",
            "loading": "Loading...",
            "saving": "Saving...",
            "saved": "Saved",
            "deleted": "Deleted",
            "updated": "Updated",
            "created": "Created",
        },
        "auth": {
            "login": "Login",
            "logout": "Logout",
            "register": "Register",
            "username": "Username",
            "password": "Password",
            "email": "Email",
            "forgot_password": "Forgot Password",
        },
        "errors": {
            "network_error": "Network Error",
            "api_error": "API Error",
            "validation_error": "Validation Error",
            "permission_denied": "Permission Denied",
            "not_found": "Not Found",
        },
    },
    "zh": {
        "nav": {
            "home": "首页",
            "dashboard": "仪表盘",
            "my_trading": "我的交易",
            "positions": "持仓",
            "history": "历史",
            "analytics": "分析",
            "charts": "图表",
            "social": "信号",
            "backtest": "回测",
            "strategies": "策略",
            "strategy_editor": "编辑器",
            "subscription": "订阅",
            "settings": "设置",
            "admin": "管理",
        },
        "common": {
            "ticker": "交易对",
            "timeframe": "周期",
            "days": "天数",
            "price": "价格",
            "direction": "方向",
            "confidence": "置信度",
            "reason": "理由",
            "name": "名称",
            "time": "时间",
            "actions": "操作",
            "status": "状态",
            "yes": "是",
            "no": "否",
            "active": "已启用",
            "draft": "草稿",
        },
        "actions": {
            "load": "加载",
            "share": "分享",
            "save": "保存",
            "reset": "重置",
            "use": "使用",
            "edit": "编辑",
            "activate": "启用",
            "deactivate": "停用",
            "delete": "删除",
            "subscribe": "订阅",
            "unsubscribe": "取消订阅",
            "follow": "关注",
        },
        "pages": {
            "charts": {
                "kicker": "市场工作台",
                "description": "查看实时市场、历史 K 线、指标、持仓标记和已执行信号标记。",
                "market_chart": "市场图表",
                "change_24h": "24小时涨跌",
                "volume_24h": "24小时成交量",
                "position_markers": "持仓标记",
                "signal_markers": "信号标记",
                "no_positions": "暂无未平仓持仓标记",
                "no_signals": "暂无已执行信号标记",
                "marker": "标记",
            },
            "social": {
                "kicker": "社区信号台",
                "description": "分享交易观点、关注信号提供者、订阅信号并查看社区活动。",
                "shared_signals": "已分享信号",
                "subscriptions": "订阅数",
                "active_providers": "活跃提供者",
                "top_ticker": "热门交易对",
                "share_signal": "分享信号",
                "leaderboard": "排行榜",
                "signal_feed": "信号流",
                "my_subscriptions": "我的信号订阅",
                "provider": "提供者",
                "no_signals": "暂无分享信号",
                "no_subscriptions": "暂无信号订阅",
                "no_leaderboard": "暂无排行榜数据",
                "signal": "信号",
                "auto_execute": "自动执行",
                "max_position": "最大仓位",
                "subscribed": "订阅时间",
            },
            "editor": {
                "kicker": "策略构建器",
                "description": "通过模板、条件、风控设置和出场规则创建可复用自定义策略。",
                "templates": "模板",
                "strategy_draft": "策略草稿",
                "entry_json": "入场条件 JSON",
                "exit_json": "出场条件 JSON",
                "risk_json": "风控 JSON",
                "tp_json": "止盈层级 JSON",
                "trailing_json": "移动止损 JSON",
                "saved_strategies": "已保存策略",
                "no_templates": "暂无可用模板",
                "no_saved_strategies": "暂无已保存自定义策略",
            },
        },
        "kpi": {
            "total_trades": "总交易数",
            "win_rate": "胜率",
            "total_pnl": "总盈亏",
            "open_positions": "持仓数量",
            "max_drawdown": "最大回撤",
            "sharpe_ratio": "夏普比率",
        },
        "trading": {
            "buy": "买入",
            "sell": "卖出",
            "long": "做多",
            "short": "做空",
            "entry": "入场",
            "exit": "出场",
            "stop_loss": "止损",
            "take_profit": "止盈",
            "trailing_stop": "移动止损",
            "pnl": "盈亏",
            "fees": "手续费",
        },
        "backtest": {
            "run_backtest": "运行回测",
            "strategy": "策略",
            "timeframe": "时间周期",
            "initial_capital": "初始资金",
            "position_size": "仓位大小",
            "winning_trades": "盈利交易",
            "losing_trades": "亏损交易",
            "profit_factor": "盈亏比",
            "max_dd": "最大回撤",
        },
        "dca": {
            "create_dca": "创建定投",
            "max_entries": "最大次数",
            "entry_spacing": "入场间距",
            "sizing_method": "仓位方式",
            "average_down": "均价下补",
            "average_up": "均价上补",
        },
        "grid": {
            "create_grid": "创建网格",
            "grid_count": "网格数量",
            "grid_spacing": "网格间距",
            "neutral": "中性",
            "long_bias": "看多",
            "short_bias": "看空",
        },
        "websocket": {
            "connected": "WebSocket已连接",
            "disconnected": "WebSocket已断开",
            "position_update": "持仓更新",
            "price_update": "价格更新",
        },
        "messages": {
            "success": "成功",
            "error": "错误",
            "warning": "警告",
            "info": "信息",
            "loading": "加载中...",
            "saving": "保存中...",
            "saved": "已保存",
            "deleted": "已删除",
            "updated": "已更新",
            "created": "已创建",
        },
        "auth": {
            "login": "登录",
            "logout": "退出",
            "register": "注册",
            "username": "用户名",
            "password": "密码",
            "email": "邮箱",
            "forgot_password": "忘记密码",
        },
        "errors": {
            "network_error": "网络错误",
            "api_error": "API错误",
            "validation_error": "验证错误",
            "permission_denied": "权限不足",
            "not_found": "未找到",
        },
    },
    "ja": {
        "nav": {
            "home": "ホーム",
            "dashboard": "ダッシュボード",
            "my_trading": "マイトレード",
            "positions": "ポジション",
            "history": "履歴",
            "analytics": "分析",
            "charts": "チャート",
            "social": "シグナル",
            "backtest": "バックテスト",
            "strategies": "ストラテジー",
            "strategy_editor": "エディター",
            "subscription": "サブスクリプション",
            "settings": "設定",
            "admin": "管理",
        },
        "common": {
            "ticker": "銘柄",
            "timeframe": "時間足",
            "days": "日数",
            "price": "価格",
            "direction": "方向",
            "confidence": "信頼度",
            "reason": "理由",
            "name": "名前",
            "time": "時間",
            "actions": "操作",
            "status": "状態",
            "yes": "はい",
            "no": "いいえ",
            "active": "有効",
            "draft": "下書き",
        },
        "actions": {
            "load": "読み込み",
            "share": "共有",
            "save": "保存",
            "reset": "リセット",
            "use": "使用",
            "edit": "編集",
            "activate": "有効化",
            "deactivate": "無効化",
            "delete": "削除",
            "subscribe": "購読",
            "unsubscribe": "購読解除",
            "follow": "フォロー",
        },
        "pages": {
            "charts": {
                "kicker": "マーケットワークスペース",
                "description": "リアルタイム市場、履歴OHLCV、指標、ポジション、実行済みシグナルを表示します。",
                "market_chart": "マーケットチャート",
                "change_24h": "24時間変化",
                "volume_24h": "24時間出来高",
                "position_markers": "ポジションマーカー",
                "signal_markers": "シグナルマーカー",
                "no_positions": "未決済ポジションマーカーはありません",
                "no_signals": "実行済みシグナルマーカーはありません",
                "marker": "マーカー",
            },
            "social": {
                "kicker": "コミュニティデスク",
                "description": "取引アイデアの共有、プロバイダーのフォロー、シグナル購読、コミュニティ活動の確認ができます。",
                "shared_signals": "共有シグナル",
                "subscriptions": "購読",
                "active_providers": "アクティブ提供者",
                "top_ticker": "上位銘柄",
                "share_signal": "シグナル共有",
                "leaderboard": "ランキング",
                "signal_feed": "シグナルフィード",
                "my_subscriptions": "自分のシグナル購読",
                "provider": "提供者",
                "no_signals": "共有シグナルはまだありません",
                "no_subscriptions": "シグナル購読はありません",
                "no_leaderboard": "ランキングデータはありません",
                "signal": "シグナル",
                "auto_execute": "自動実行",
                "max_position": "最大ポジション",
                "subscribed": "購読日時",
            },
            "editor": {
                "kicker": "ストラテジービルダー",
                "description": "テンプレート、条件、リスク設定、決済ルールから再利用可能なカスタム戦略を作成します。",
                "templates": "テンプレート",
                "strategy_draft": "戦略ドラフト",
                "entry_json": "エントリー条件 JSON",
                "exit_json": "決済条件 JSON",
                "risk_json": "リスク JSON",
                "tp_json": "利確レベル JSON",
                "trailing_json": "トレーリングストップ JSON",
                "saved_strategies": "保存済み戦略",
                "no_templates": "利用可能なテンプレートはありません",
                "no_saved_strategies": "保存済みカスタム戦略はありません",
            },
        },
        "kpi": {
            "total_trades": "総取引数",
            "win_rate": "勝率",
            "total_pnl": "総損益",
            "open_positions": "ポジション数",
            "max_drawdown": "最大ドローダウン",
            "sharpe_ratio": "シャープレシオ",
        },
        "trading": {
            "buy": "買い",
            "sell": "売り",
            "long": "ロング",
            "short": "ショート",
            "entry": "エントリー",
            "exit": "退出",
            "stop_loss": "ストップロス",
            "take_profit": "テイクプロフィット",
            "trailing_stop": "トレイリングストップ",
            "pnl": "損益",
            "fees": "手数料",
        },
        "messages": {
            "success": "成功",
            "error": "エラー",
            "warning": "警告",
            "info": "情報",
            "loading": "読み込み中...",
            "saving": "保存中...",
            "saved": "保存完了",
            "deleted": "削除完了",
            "updated": "更新完了",
            "created": "作成完了",
        },
    },
    "ko": {
        "nav": {
            "home": "홈",
            "dashboard": "대시보드",
            "my_trading": "나의 거래",
            "positions": "포지션",
            "history": "히스토리",
            "analytics": "분석",
            "charts": "차트",
            "social": "시그널",
            "backtest": "백테스트",
            "strategies": "스트래티지",
            "strategy_editor": "에디터",
            "subscription": "구독",
            "settings": "설정",
            "admin": "관리",
        },
        "common": {
            "ticker": "티커",
            "timeframe": "시간대",
            "days": "일수",
            "price": "가격",
            "direction": "방향",
            "confidence": "신뢰도",
            "reason": "이유",
            "name": "이름",
            "time": "시간",
            "actions": "작업",
            "status": "상태",
            "yes": "예",
            "no": "아니요",
            "active": "활성",
            "draft": "초안",
        },
        "actions": {
            "load": "불러오기",
            "share": "공유",
            "save": "저장",
            "reset": "초기화",
            "use": "사용",
            "edit": "편집",
            "activate": "활성화",
            "deactivate": "비활성화",
            "delete": "삭제",
            "subscribe": "구독",
            "unsubscribe": "구독 취소",
            "follow": "팔로우",
        },
        "pages": {
            "charts": {
                "kicker": "마켓 워크스페이스",
                "description": "실시간 시장, 과거 OHLCV, 지표, 포지션, 실행된 시그널 마커를 확인합니다.",
                "market_chart": "마켓 차트",
                "change_24h": "24시간 변동",
                "volume_24h": "24시간 거래량",
                "position_markers": "포지션 마커",
                "signal_markers": "시그널 마커",
                "no_positions": "열린 포지션 마커가 없습니다",
                "no_signals": "실행된 시그널 마커가 없습니다",
                "marker": "마커",
            },
            "social": {
                "kicker": "커뮤니티 데스크",
                "description": "거래 아이디어를 공유하고 제공자를 팔로우하며 시그널을 구독하고 커뮤니티 활동을 확인합니다.",
                "shared_signals": "공유 시그널",
                "subscriptions": "구독",
                "active_providers": "활성 제공자",
                "top_ticker": "상위 티커",
                "share_signal": "시그널 공유",
                "leaderboard": "리더보드",
                "signal_feed": "시그널 피드",
                "my_subscriptions": "내 시그널 구독",
                "provider": "제공자",
                "no_signals": "공유된 시그널이 없습니다",
                "no_subscriptions": "시그널 구독이 없습니다",
                "no_leaderboard": "리더보드 데이터가 없습니다",
                "signal": "시그널",
                "auto_execute": "자동 실행",
                "max_position": "최대 포지션",
                "subscribed": "구독 시간",
            },
            "editor": {
                "kicker": "전략 빌더",
                "description": "템플릿, 조건, 리스크 설정, 종료 규칙으로 재사용 가능한 사용자 전략을 만듭니다.",
                "templates": "템플릿",
                "strategy_draft": "전략 초안",
                "entry_json": "진입 조건 JSON",
                "exit_json": "종료 조건 JSON",
                "risk_json": "리스크 JSON",
                "tp_json": "익절 레벨 JSON",
                "trailing_json": "트레일링 스탑 JSON",
                "saved_strategies": "저장된 전략",
                "no_templates": "사용 가능한 템플릿이 없습니다",
                "no_saved_strategies": "저장된 사용자 전략이 없습니다",
            },
        },
        "kpi": {
            "total_trades": "총 거래",
            "win_rate": "승률",
            "total_pnl": "총 손익",
            "open_positions": "포지션 수",
            "max_drawdown": "최대 낙폭",
            "sharpe_ratio": "샤프 비율",
        },
        "trading": {
            "buy": "매수",
            "sell": "매도",
            "long": "롱",
            "short": "숏",
            "entry": "진입",
            "exit": "청산",
            "stop_loss": "스탑로스",
            "take_profit": "테이크프로핏",
            "trailing_stop": "트레일링스탑",
            "pnl": "손익",
            "fees": "수수료",
        },
        "messages": {
            "success": "성공",
            "error": "오류",
            "warning": "경고",
            "info": "정보",
            "loading": "로딩...",
            "saving": "저장...",
            "saved": "저장완료",
            "deleted": "삭제완료",
            "updated": "업데이트완료",
            "created": "생성완료",
        },
    },
    "es": {
        "nav": {
            "home": "Inicio",
            "dashboard": "Panel",
            "my_trading": "Mi Trading",
            "positions": "Posiciones",
            "history": "Historial",
            "analytics": "Análisis",
            "charts": "Gráficos",
            "social": "Señales",
            "backtest": "Backtest",
            "strategies": "Estrategias",
            "strategy_editor": "Editor",
            "subscription": "Suscripción",
            "settings": "Configuración",
            "admin": "Admin",
        },
        "common": {
            "ticker": "Ticker",
            "timeframe": "Temporalidad",
            "days": "Días",
            "price": "Precio",
            "direction": "Dirección",
            "confidence": "Confianza",
            "reason": "Motivo",
            "name": "Nombre",
            "time": "Hora",
            "actions": "Acciones",
            "status": "Estado",
            "yes": "Sí",
            "no": "No",
            "active": "activo",
            "draft": "borrador",
        },
        "actions": {
            "load": "Cargar",
            "share": "Compartir",
            "save": "Guardar",
            "reset": "Restablecer",
            "use": "Usar",
            "edit": "Editar",
            "activate": "Activar",
            "deactivate": "Desactivar",
            "delete": "Eliminar",
            "subscribe": "Suscribirse",
            "unsubscribe": "Cancelar suscripción",
            "follow": "Seguir",
        },
        "pages": {
            "charts": {
                "kicker": "Espacio de mercado",
                "description": "Contexto de mercado en vivo, OHLCV histórico, indicadores, posiciones abiertas y marcas de señales ejecutadas.",
                "market_chart": "Gráfico de mercado",
                "change_24h": "Cambio 24h",
                "volume_24h": "Volumen 24h",
                "position_markers": "Marcas de posición",
                "signal_markers": "Marcas de señal",
                "no_positions": "No hay marcas de posiciones abiertas",
                "no_signals": "No hay marcas de señales ejecutadas",
                "marker": "Marca",
            },
            "social": {
                "kicker": "Mesa comunitaria",
                "description": "Comparte ideas, sigue proveedores, suscríbete a señales y revisa la actividad de la comunidad.",
                "shared_signals": "Señales compartidas",
                "subscriptions": "Suscripciones",
                "active_providers": "Proveedores activos",
                "top_ticker": "Ticker principal",
                "share_signal": "Compartir señal",
                "leaderboard": "Clasificación",
                "signal_feed": "Feed de señales",
                "my_subscriptions": "Mis suscripciones de señales",
                "provider": "Proveedor",
                "no_signals": "Aún no hay señales compartidas",
                "no_subscriptions": "No hay suscripciones de señales",
                "no_leaderboard": "No hay datos de clasificación",
                "signal": "Señal",
                "auto_execute": "Ejecución automática",
                "max_position": "Posición máxima",
                "subscribed": "Suscrito",
            },
            "editor": {
                "kicker": "Constructor de estrategias",
                "description": "Crea estrategias reutilizables con plantillas, condiciones, riesgo y reglas de salida.",
                "templates": "Plantillas",
                "strategy_draft": "Borrador de estrategia",
                "entry_json": "Condiciones de entrada JSON",
                "exit_json": "Condiciones de salida JSON",
                "risk_json": "Riesgo JSON",
                "tp_json": "Niveles TP JSON",
                "trailing_json": "Trailing Stop JSON",
                "saved_strategies": "Estrategias guardadas",
                "no_templates": "No hay plantillas disponibles",
                "no_saved_strategies": "No hay estrategias personalizadas guardadas",
            },
        },
        "kpi": {
            "total_trades": "Total Trades",
            "win_rate": "Win Rate",
            "total_pnl": "Total PnL",
            "open_positions": "Posiciones Abiertas",
            "max_drawdown": "Max Drawdown",
            "sharpe_ratio": "Sharpe Ratio",
        },
        "trading": {
            "buy": "Comprar",
            "sell": "Vender",
            "long": "Long",
            "short": "Short",
            "entry": "Entrada",
            "exit": "Salida",
            "stop_loss": "Stop Loss",
            "take_profit": "Take Profit",
            "trailing_stop": "Trailing Stop",
            "pnl": "PnL",
            "fees": "Comisiones",
        },
        "messages": {
            "success": "Éxito",
            "error": "Error",
            "warning": "Advertencia",
            "info": "Info",
            "loading": "Cargando...",
            "saving": "Guardando...",
            "saved": "Guardado",
            "deleted": "Eliminado",
            "updated": "Actualizado",
            "created": "Creado",
        },
    },
}


class LanguageRequest(BaseModel):
    language: str = Field(..., description="Language code (en, zh, ja, ko, es)")


@router.get("/languages")
async def list_supported_languages():
    """List all supported languages."""
    return {
        "languages": _SUPPORTED_LANGUAGES,
        "default": "en",
        "count": len(_SUPPORTED_LANGUAGES),
    }


@router.get("/public/translations/{language}")
async def get_public_translations(language: str):
    """Get translations for public pages that are available before login."""
    if language not in _TRANSLATIONS:
        raise HTTPException(404, f"Language '{language}' not supported")
    return {"language": language, "translations": _TRANSLATIONS[language]}


@router.get("/translations/{language}")
async def get_translations(
    language: str,
    section: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """Get translations for a specific language."""
    if language not in _TRANSLATIONS:
        raise HTTPException(404, f"Language '{language}' not supported")

    translations = _TRANSLATIONS[language]

    if section:
        if section not in translations:
            raise HTTPException(404, f"Section '{section}' not found in translations")
        return {"language": language, "section": section, "translations": translations[section]}

    return {"language": language, "translations": translations}


@router.get("/user/language")
async def get_user_language(
    user: dict = Depends(get_current_user),
):
    """Get user's preferred language setting."""
    user_lang = user.get("language", "en")

    if user_lang not in _SUPPORTED_LANGUAGES:
        user_lang = "en"

    return {
        "language": user_lang,
        "name": _SUPPORTED_LANGUAGES.get(user_lang, "English"),
        "supported": list(_SUPPORTED_LANGUAGES.keys()),
    }


@router.post("/user/language")
async def set_user_language(
    request: LanguageRequest,
    user: dict = Depends(get_current_user),
):
    """Set user's preferred language."""
    if request.language not in _SUPPORTED_LANGUAGES:
        raise HTTPException(400, f"Unsupported language: {request.language}")

    logger.info(f"[i18n] User {user.get('id')} set language to {request.language}")

    return {
        "status": "updated",
        "language": request.language,
        "name": _SUPPORTED_LANGUAGES[request.language],
    }


@router.get("/detect")
async def detect_browser_language(
    accept_language: str = "",
):
    """Detect language from browser Accept-Language header."""
    if not accept_language:
        return {"detected": "en", "fallback": True}

    languages = []
    for lang in accept_language.split(","):
        code = lang.split("-")[0].strip().lower()
        weight = 1.0
        if ";" in lang:
            weight = float(lang.split("q=")[1]) if "q=" in lang else 1.0
        languages.append((code, weight))

    languages.sort(key=lambda x: x[1], reverse=True)

    for code, _ in languages:
        if code in _SUPPORTED_LANGUAGES:
            return {"detected": code, "fallback": False}

    return {"detected": "en", "fallback": True}


@router.get("/translate/{key}")
async def translate_single_key(
    key: str,
    language: str = "en",
    user: dict = Depends(get_current_user),
):
    """Translate a single key."""
    if language not in _TRANSLATIONS:
        language = "en"

    parts = key.split(".")
    current = _TRANSLATIONS[language]

    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return {"key": key, "language": language, "translation": key, "found": False}

    return {"key": key, "language": language, "translation": current, "found": True}


@router.get("/bulk")
async def translate_bulk_keys(
    keys: str,
    language: str = "en",
    user: dict = Depends(get_current_user),
):
    """Translate multiple keys at once."""
    if language not in _TRANSLATIONS:
        language = "en"

    key_list = keys.split(",")
    results = {}

    for key in key_list:
        parts = key.strip().split(".")
        current = _TRANSLATIONS[language]

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                current = key
                break

        results[key] = current

    return {"language": language, "translations": results}


@router.get("/all/{language}")
async def get_all_translations(
    language: str,
    user: dict = Depends(get_current_user),
):
    """Get all translations for a language as JSON file."""
    if language not in _TRANSLATIONS:
        raise HTTPException(404, f"Language '{language}' not supported")

    return {
        "format": "json",
        "language": language,
        "content": json.dumps(_TRANSLATIONS[language], indent=2),
    }
