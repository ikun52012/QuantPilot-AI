"""
QuantPilot AI - AI Configuration Router
Admin endpoints for AI provider catalog and experimental voting settings.
"""
import json
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from loguru import logger

from core.auth import require_admin, get_current_user
from core.config import settings
from core.database import db_manager, set_admin_setting, get_admin_setting
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db


router = APIRouter(prefix="/api/admin/ai", tags=["ai-config"])


def _parse_model_id(model_id: str) -> tuple[str, str]:
    """Parse model ID in format 'provider/model_name' or legacy 'provider:model_name'."""
    model_id = model_id.strip().lower()

    if "/" in model_id:
        parts = model_id.split("/", 1)
        return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""

    if ":" in model_id:
        parts = model_id.split(":", 1)
        return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""

    legacy_providers = {"openai", "anthropic", "deepseek", "openrouter", "custom", "mistral"}
    if model_id in legacy_providers:
        return model_id, ""

    return "openrouter", model_id


class VotingConfigRequest(BaseModel):
    """Request to update voting configuration."""
    enabled: bool = Field(description="Enable/disable stored voting configuration")
    models: List[str] = Field(default_factory=list, description="List of models in format provider/model_name")
    weights: Dict[str, float] = Field(default_factory=dict, description="Weight for each model (should sum to ~1.0)")
    strategy: str = Field(default="weighted", description="Voting strategy: weighted/consensus/best_confidence")


class VotingConfigResponse(BaseModel):
    """Current voting configuration."""
    enabled: bool
    models: List[str]
    weights: Dict[str, float]
    strategy: str
    available_providers: Dict[str, List[str]]
    current_provider: str
    openrouter_enabled: bool
    openrouter_model: str
    custom_provider_enabled: bool


class ProviderConfigRequest(BaseModel):
    """Request to update provider configuration."""
    provider: str = Field(description="Primary AI provider")
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    anthropic_model: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    deepseek_model: Optional[str] = None
    mistral_api_key: Optional[str] = None
    mistral_model: Optional[str] = None
    openrouter_enabled: Optional[bool] = None
    openrouter_api_key: Optional[str] = None
    openrouter_model: Optional[str] = None
    custom_provider_enabled: Optional[bool] = None
    custom_provider_name: Optional[str] = None
    custom_provider_api_key: Optional[str] = None
    custom_provider_model: Optional[str] = None
    custom_provider_api_url: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


@router.get("/voting-config")
async def get_voting_config(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Get current stored voting configuration.

    Returns:
        - enabled: Whether voting configuration is enabled
        - models: List of configured models
        - weights: Model weights
        - strategy: Voting strategy
        - available_providers: Available models per provider
        - current_provider: Primary provider
        - openrouter_enabled: Whether OpenRouter is enabled
        - openrouter_model: Current OpenRouter model
        - custom_provider_enabled: Whether custom provider is enabled

    Note: the active analyzer currently executes the primary provider path.
    These settings are persisted for voting-capable deployments or future use.
    """
    return VotingConfigResponse(
        enabled=settings.ai.voting_enabled,
        models=settings.ai.voting_models,
        weights=settings.ai.voting_weights,
        strategy=settings.ai.voting_strategy,
        available_providers=settings.ai.available_models,
        current_provider=settings.ai.provider,
        openrouter_enabled=settings.ai.openrouter_enabled,
        openrouter_model=settings.ai.openrouter_model,
        custom_provider_enabled=settings.ai.custom_provider_enabled,
    )


@router.post("/voting-config")
async def update_voting_config(
    req: VotingConfigRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """
    Update stored voting configuration.

    Model ID format (use slash separator):
    - 'openai/gpt-4o' - OpenAI GPT-4o
    - 'openai/gpt-4o-mini' - OpenAI GPT-4o-mini
    - 'anthropic/claude-3-5-sonnet-latest' - Anthropic Claude 3.5 Sonnet
    - 'anthropic/claude-3-5-haiku-latest' - Anthropic Claude 3.5 Haiku
    - 'deepseek/deepseek-chat' - DeepSeek Chat
    - 'deepseek/deepseek-reasoner' - DeepSeek Reasoner
    - 'openrouter/openai/gpt-4o' - GPT-4o via OpenRouter
    - 'openrouter/anthropic/claude-3.5-sonnet' - Claude via OpenRouter
    - 'openrouter/google/gemini-pro-1.5' - Gemini via OpenRouter
    - 'openrouter/meta-llama/llama-3.1-70b-instruct' - Llama via OpenRouter
    - 'openrouter/mistralai/mistral-large' - Mistral via OpenRouter
    - 'openrouter/qwen/qwen-2.5-72b-instruct' - Qwen via OpenRouter
    - 'custom/<model_name>' - Custom provider model
    - 'local' - Local rule-based fallback (no API call)

    Legacy format also supported: 'provider:model_name' (colon separator)

    Voting strategies:
    - **weighted**: Weighted average of confidence, vote on recommendation (recommended)
    - **consensus**: Only proceed if majority (>50%) votes execute
    - **best_confidence**: Take result from highest confidence model

    Example weights: {"openai/gpt-4o": 0.4, "deepseek/deepseek-chat": 0.3, "local": 0.3}
    """
    valid_models = []
    for model_id in req.models:
        model_id = model_id.strip()

        if model_id == "local":
            valid_models.append(model_id)
            continue

        provider, model_name = _parse_model_id(model_id)

        valid_providers = ["openai", "anthropic", "deepseek", "openrouter", "custom", "mistral"]
        if provider in valid_providers:
            normalized_id = f"{provider}/{model_name}" if model_name else provider
            valid_models.append(normalized_id)
        else:
            logger.warning(f"[AI Config] Invalid model ID format: {model_id}")

    if not valid_models:
        raise HTTPException(400, "No valid models specified")

    # Validate strategy
    if req.strategy not in ["weighted", "consensus", "best_confidence"]:
        raise HTTPException(400, "Invalid voting strategy")

    normalized_weights = {}
    for model_id, weight in req.weights.items():
        if model_id == "local":
            normalized_weights[model_id] = float(weight)
        else:
            provider, model_name = _parse_model_id(model_id)
            normalized_key = f"{provider}/{model_name}" if model_name else provider
            normalized_weights[normalized_key] = float(weight)

    total_weight = sum(normalized_weights.values())
    if normalized_weights and abs(total_weight - 1.0) > 0.15:
        logger.warning(f"[AI Config] Weights sum to {total_weight}, should be ~1.0")

    await set_admin_setting(db, "ai_voting_enabled", json.dumps(req.enabled))
    await set_admin_setting(db, "ai_voting_models", json.dumps(valid_models))
    await set_admin_setting(db, "ai_voting_weights", json.dumps(normalized_weights))
    await set_admin_setting(db, "ai_voting_strategy", req.strategy)
    await db.commit()

    settings.ai.voting_enabled = req.enabled
    settings.ai.voting_models = valid_models
    settings.ai.voting_weights = normalized_weights
    settings.ai.voting_strategy = req.strategy

    logger.info(f"[AI Config] Voting config updated by {admin['username']}: enabled={req.enabled}, models={valid_models}")

    return {
        "status": "success",
        "message": "Voting configuration updated",
        "config": {
            "enabled": req.enabled,
            "models": valid_models,
            "weights": req.weights,
            "strategy": req.strategy,
        }
    }


@router.get("/provider-config")
async def get_provider_config(
    admin: dict = Depends(require_admin),
):
    """
    Get current AI provider configuration.

    Returns all provider settings including API keys (masked).
    """
    return {
        "provider": settings.ai.provider,
        "providers": {
            "openai": {
                "enabled": bool(settings.ai.openai_api_key),
                "model": settings.ai.openai_model,
                "available_models": settings.ai.available_models.get("openai", []),
            },
            "anthropic": {
                "enabled": bool(settings.ai.anthropic_api_key),
                "model": settings.ai.anthropic_model,
                "available_models": settings.ai.available_models.get("anthropic", []),
            },
            "deepseek": {
                "enabled": bool(settings.ai.deepseek_api_key),
                "model": settings.ai.deepseek_model,
                "available_models": settings.ai.available_models.get("deepseek", []),
            },
            "mistral": {
                "enabled": bool(settings.ai.mistral_api_key),
                "model": settings.ai.mistral_model,
                "available_models": settings.ai.available_models.get("mistral", []),
            },
            "openrouter": {
                "enabled": settings.ai.openrouter_enabled and bool(settings.ai.openrouter_api_key),
                "model": settings.ai.openrouter_model,
                "available_models": settings.ai.available_models.get("openrouter", []),
                "site_url": settings.ai.openrouter_site_url,
                "app_name": settings.ai.openrouter_app_name,
            },
            "custom": {
                "enabled": settings.ai.custom_provider_enabled,
                "name": settings.ai.custom_provider_name,
                "model": settings.ai.custom_provider_model,
                "url": settings.ai.custom_provider_api_url,
            },
        },
        "common": {
            "temperature": settings.ai.temperature,
            "max_tokens": settings.ai.max_tokens,
        },
    }


@router.post("/provider-config")
async def update_provider_config(
    req: ProviderConfigRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Update AI provider configuration.

    OpenRouter provider uses OpenAI-compatible model IDs through a single API:
    - OpenAI: openai/gpt-4o, openai/gpt-4o-mini
    - Anthropic: anthropic/claude-3.5-sonnet
    - Google: google/gemini-pro-1.5
    - Meta: meta-llama/llama-3.1-70b-instruct
    - Mistral: mistralai/mistral-large
    - DeepSeek: deepseek/deepseek-chat
    - Qwen: qwen/qwen-2.5-72b-instruct

    This endpoint configures provider routing; execution still follows the selected primary provider.
    """
    # Update settings
    if req.provider:
        if req.provider not in ["openai", "anthropic", "deepseek", "openrouter", "custom", "mistral"]:
            raise HTTPException(400, "Invalid provider")
        settings.ai.provider = req.provider
        await set_admin_setting(db, "ai_provider", req.provider)

    # Update provider-specific settings
    if req.openai_api_key:
        settings.ai.openai_api_key = req.openai_api_key
        await set_admin_setting(db, "openai_api_key", req.openai_api_key)
    if req.openai_model:
        settings.ai.openai_model = req.openai_model
        await set_admin_setting(db, "openai_model", req.openai_model)

    if req.anthropic_api_key:
        settings.ai.anthropic_api_key = req.anthropic_api_key
        await set_admin_setting(db, "anthropic_api_key", req.anthropic_api_key)
    if req.anthropic_model:
        settings.ai.anthropic_model = req.anthropic_model
        await set_admin_setting(db, "anthropic_model", req.anthropic_model)

    if req.deepseek_api_key:
        settings.ai.deepseek_api_key = req.deepseek_api_key
        await set_admin_setting(db, "deepseek_api_key", req.deepseek_api_key)
    if req.deepseek_model:
        settings.ai.deepseek_model = req.deepseek_model
        await set_admin_setting(db, "deepseek_model", req.deepseek_model)

    # Mistral
    if req.mistral_api_key:
        settings.ai.mistral_api_key = req.mistral_api_key
        await set_admin_setting(db, "mistral_api_key", req.mistral_api_key)
    if req.mistral_model:
        settings.ai.mistral_model = req.mistral_model
        await set_admin_setting(db, "mistral_model", req.mistral_model)

    # OpenRouter
    if req.openrouter_enabled is not None:
        settings.ai.openrouter_enabled = req.openrouter_enabled
        await set_admin_setting(db, "openrouter_enabled", json.dumps(req.openrouter_enabled))
    if req.openrouter_api_key:
        settings.ai.openrouter_api_key = req.openrouter_api_key
        await set_admin_setting(db, "openrouter_api_key", req.openrouter_api_key)
    if req.openrouter_model:
        settings.ai.openrouter_model = req.openrouter_model
        await set_admin_setting(db, "openrouter_model", req.openrouter_model)

    # Custom provider
    if req.custom_provider_enabled is not None:
        settings.ai.custom_provider_enabled = req.custom_provider_enabled
        await set_admin_setting(db, "custom_ai_provider_enabled", json.dumps(req.custom_provider_enabled))
    if req.custom_provider_name:
        settings.ai.custom_provider_name = req.custom_provider_name
        await set_admin_setting(db, "custom_ai_provider_name", req.custom_provider_name)
    if req.custom_provider_api_key:
        settings.ai.custom_provider_api_key = req.custom_provider_api_key
        await set_admin_setting(db, "custom_ai_api_key", req.custom_provider_api_key)
    if req.custom_provider_model:
        settings.ai.custom_provider_model = req.custom_provider_model
        await set_admin_setting(db, "custom_ai_model", req.custom_provider_model)
    if req.custom_provider_api_url:
        settings.ai.custom_provider_api_url = req.custom_provider_api_url
        await set_admin_setting(db, "custom_ai_api_url", req.custom_provider_api_url)

    # Common settings
    if req.temperature is not None:
        settings.ai.temperature = req.temperature
        await set_admin_setting(db, "ai_temperature", str(req.temperature))
    if req.max_tokens is not None:
        settings.ai.max_tokens = req.max_tokens
        await set_admin_setting(db, "ai_max_tokens", str(req.max_tokens))

    await db.commit()

    logger.info(f"[AI Config] Provider config updated by {admin['username']}")

    return {
        "status": "success",
        "message": "Provider configuration updated",
        "provider": settings.ai.provider,
    }


@router.get("/models-list")
async def get_available_models(
    admin: dict = Depends(require_admin),
):
    """
    Get list of all available models across providers.

    Returns complete model catalog for selection in voting configuration.
    """
    return {
        "providers": settings.ai.available_models,
        "openrouter_popular": [
            {"id": "openai/gpt-4o", "name": "GPT-4o", "provider": "OpenAI", "pricing": "OpenRouter route"},
            {"id": "anthropic/claude-3.5-sonnet", "name": "Claude 3.5 Sonnet", "provider": "Anthropic", "pricing": "OpenRouter route"},
            {"id": "google/gemini-pro-1.5", "name": "Gemini Pro 1.5", "provider": "Google", "pricing": "OpenRouter route"},
            {"id": "meta-llama/llama-3.1-70b-instruct", "name": "Llama 3.1 70B", "provider": "Meta", "pricing": "OpenRouter route"},
            {"id": "mistralai/mistral-large", "name": "Mistral Large", "provider": "Mistral", "pricing": "OpenRouter route"},
            {"id": "deepseek/deepseek-chat", "name": "DeepSeek Chat", "provider": "DeepSeek", "pricing": "OpenRouter route"},
            {"id": "qwen/qwen-2.5-72b-instruct", "name": "Qwen 2.5 72B", "provider": "Alibaba", "pricing": "OpenRouter route"},
        ],
        "description": """
## Voting Configuration Guide

### Current Status
Voting settings are stored for compatible deployments. The built-in analyzer currently executes the selected primary provider.

### Use Cases
- Keep a model catalog ready for future voting execution
- Store operator-preferred model groups
- Document the intended fallback mix

### Voting Strategies
1. Weighted: Weighted average of confidence
2. Consensus: Only proceed if majority agrees
3. Best Confidence: Take highest confidence result

### Recommended Configuration
- 1 high-quality model (OpenAI/Anthropic): 40-50% weight
- 1 cost-effective model (DeepSeek): 30-40% weight
- 1 local fallback: 10-20% weight
"""
    }
