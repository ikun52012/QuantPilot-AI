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
            "positions": "Positions",
            "history": "History",
            "analytics": "Analytics",
            "backtest": "Backtest",
            "strategies": "Strategies",
            "settings": "Settings",
            "admin": "Admin",
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
            "positions": "持仓",
            "history": "历史",
            "analytics": "分析",
            "backtest": "回测",
            "strategies": "策略",
            "settings": "设置",
            "admin": "管理",
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
            "positions": "ポジション",
            "history": "履歴",
            "analytics": "分析",
            "backtest": "バックテスト",
            "strategies": "ストラテジー",
            "settings": "設定",
            "admin": "管理",
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
            "positions": "포지션",
            "history": "히스토리",
            "analytics": "분석",
            "backtest": "백테스트",
            "strategies": "스트래티지",
            "settings": "설정",
            "admin": "관리",
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
            "positions": "Posiciones",
            "history": "Historial",
            "analytics": "Análisis",
            "backtest": "Backtest",
            "strategies": "Estrategias",
            "settings": "Configuración",
            "admin": "Admin",
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
