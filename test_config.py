#!/usr/bin/env python3
"""
Quick test to verify custom AI provider configuration.
"""

import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings

print("=== Testing Custom AI Provider Configuration ===\n")

# Test 1: Check if settings are loaded
print("1. Basic Configuration:")
print(f"   AI Provider: {settings.ai.provider}")
print(f"   Default Provider: {settings.ai.provider}")

# Test 2: Check custom provider fields exist
print("\n2. Custom Provider Fields:")
print(f"   custom_provider_enabled: {settings.ai.custom_provider_enabled}")
print(f"   custom_provider_name: {settings.ai.custom_provider_name}")
print(f"   custom_provider_model: {settings.ai.custom_provider_model}")
print(f"   custom_provider_api_url: {settings.ai.custom_provider_api_url}")
print(f"   custom_provider_api_key set: {'Yes' if settings.ai.custom_provider_api_key else 'No'}")

# Test 3: Check if custom provider can be enabled
print("\n3. Custom Provider Activation Test:")
if settings.ai.custom_provider_enabled:
    print("   ✅ Custom provider is enabled")
    if settings.ai.provider == settings.ai.custom_provider_name:
        print("   ✅ AI provider is set to custom provider name")
    else:
        print(f"   ⚠️  AI provider ({settings.ai.provider}) doesn't match custom provider name ({settings.ai.custom_provider_name})")
else:
    print("   ⚠️  Custom provider is not enabled (this is normal for default config)")

# Test 4: Check if all required imports are available
print("\n4. Import Test:")
try:
    from ai_analyzer import analyze_signal, _call_custom
    print("   ✅ All AI analyzer imports successful")
except ImportError as e:
    print(f"   ❌ Import error: {e}")

print("\n=== Configuration Test Complete ===")
print("\nTo enable custom AI provider:")
print("1. Set CUSTOM_AI_PROVIDER_ENABLED=true in .env")
print("2. Set AI_PROVIDER to match CUSTOM_AI_PROVIDER_NAME")
print("3. Configure CUSTOM_AI_API_URL and CUSTOM_AI_API_KEY")