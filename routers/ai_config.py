"""
QuantPilot AI - AI Configuration Router
Admin endpoints for AI provider and multi-model voting configuration.
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


class VotingConfigRequest(BaseModel):
    """Request to update voting configuration."""
    enabled: bool = Field(description="Enable/disable multi-model voting")
    models: List[str] = Field(default_factory=list, description="List of models to participate in voting")
    weights: Dict[str, float] = Field(default_factory=dict, description="Weight for each model")
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
    Get current multi-model voting configuration.
    
    Returns:
        - enabled: Whether voting is enabled
        - models: List of participating models
        - weights: Model weights
        - strategy: Voting strategy
        - available_providers: Available models per provider
        - current_provider: Primary provider
        - openrouter_enabled: Whether OpenRouter is enabled
        - openrouter_model: Current OpenRouter model
        - custom_provider_enabled: Whether custom provider is enabled
    
    Multi-model voting allows combining analysis from multiple AI providers
    to get more robust trading decisions.
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
    Update multi-model voting configuration.
    
    Model ID format:
    - 'openai:gpt-4o' - OpenAI with specific model
    - 'anthropic:claude-3.5-sonnet' - Anthropic with specific model
    - 'deepseek:deepseek-chat' - DeepSeek
    - 'openrouter:openai/gpt-4o' - OpenRouter (100+ models via single API)
    - 'openrouter:anthropic/claude-3.5-sonnet' - Claude via OpenRouter
    - 'openrouter:google/gemini-pro-1.5' - Gemini via OpenRouter
    - 'local' - Local rule-based fallback
    
    Voting strategies:
    - **weighted**: Weighted average of confidence, vote on recommendation (recommended)
    - **consensus**: Only proceed if majority agrees
    - **best_confidence**: Take result from highest confidence model
    
    Example weights: {"openai:gpt-4o": 0.4, "deepseek:deepseek-chat": 0.3, "local": 0.3}
    """
    # Validate models
    valid_models = []
    for model_id in req.models:
        if model_id == "local":
            valid_models.append(model_id)
            continue
        
        if ":" in model_id:
            provider, model = model_id.split(":", 1)
            if provider in ["openai", "anthropic", "deepseek", "openrouter", "custom"]:
                valid_models.append(model_id)
        elif model_id in ["openai", "anthropic", "deepseek", "openrouter", "custom"]:
            valid_models.append(model_id)
    
    if not valid_models:
        raise HTTPException(400, "No valid models specified")
    
    # Validate strategy
    if req.strategy not in ["weighted", "consensus", "best_confidence"]:
        raise HTTPException(400, "Invalid voting strategy")
    
    # Validate weights sum to ~1.0
    if req.weights:
        total_weight = sum(req.weights.values())
        if abs(total_weight - 1.0) > 0.1:
            logger.warning(f"[AI Config] Weights sum to {total_weight}, should be ~1.0")
    
    # Save to database (persisted settings)
    await set_admin_setting(db, "ai_voting_enabled", json.dumps(req.enabled))
    await set_admin_setting(db, "ai_voting_models", json.dumps(valid_models))
    await set_admin_setting(db, "ai_voting_weights", json.dumps(req.weights))
    await set_admin_setting(db, "ai_voting_strategy", req.strategy)
    await db.commit()
    
    # Update runtime settings
    settings.ai.voting_enabled = req.enabled
    settings.ai.voting_models = valid_models
    settings.ai.voting_weights = req.weights
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
    
    OpenRouter provider allows access to 100+ models through a single API:
    - OpenAI: openai/gpt-4o, openai/gpt-4o-mini
    - Anthropic: anthropic/claude-3.5-sonnet
    - Google: google/gemini-pro-1.5
    - Meta: meta-llama/llama-3.1-70b-instruct
    - Mistral: mistralai/mistral-large
    - DeepSeek: deepseek/deepseek-chat
    - Qwen: qwen/qwen-2.5-72b-instruct
    
    Cost-effective way to enable multi-model voting with single API key.
    """
    # Update settings
    if req.provider:
        if req.provider not in ["openai", "anthropic", "deepseek", "openrouter", "custom"]:
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
            {"id": "openai/gpt-4o", "name": "GPT-4o", "provider": "OpenAI", "pricing": "$5/1M input"},
            {"id": "anthropic/claude-3.5-sonnet", "name": "Claude 3.5 Sonnet", "provider": "Anthropic", "pricing": "$3/1M input"},
            {"id": "google/gemini-pro-1.5", "name": "Gemini Pro 1.5", "provider": "Google", "pricing": "$1.25/1M input"},
            {"id": "meta-llama/llama-3.1-70b-instruct", "name": "Llama 3.1 70B", "provider": "Meta", "pricing": "$0.9/1M input"},
            {"id": "mistralai/mistral-large", "name": "Mistral Large", "provider": "Mistral", "pricing": "$2/1M input"},
            {"id": "deepseek/deepseek-chat", "name": "DeepSeek Chat", "provider": "DeepSeek", "pricing": "$0.14/1M input"},
            {"id": "qwen/qwen-2.5-72b-instruct", "name": "Qwen 2.5 72B", "provider": "Alibaba", "pricing": "$0.35/1M input"},
        ],
        "description": """
## Multi-Model Voting Configuration Guide

### What is Multi-Model Voting?
Multi-model voting combines analysis from multiple AI providers to produce more robust trading decisions.

### Benefits
- Reduced bias: Different models have different strengths
- Increased reliability: Fallback if one model fails
- Better accuracy: Weighted combination often outperforms single models
- Cost optimization: Use cheaper models for most queries

### Voting Strategies
1. Weighted (Recommended): Weighted average of confidence
2. Consensus: Only proceed if majority agrees
3. Best Confidence: Take highest confidence result

### Recommended Configuration
- 1 high-quality model (OpenAI/Anthropic): 40-50% weight
- 1 cost-effective model (DeepSeek): 30-40% weight
- 1 local fallback: 10-20% weight
"""
    }