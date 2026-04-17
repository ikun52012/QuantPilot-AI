#!/usr/bin/env python3
"""
Simple test to verify the configuration structure.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=== Testing Configuration Structure ===\n")

try:
    # Test if we can import the config module
    import config
    print("✅ config.py imports successfully")
    
    # Test if settings object exists
    if hasattr(config, 'settings'):
        print("✅ config.settings exists")
        
        # Test AI configuration
        if hasattr(config.settings, 'ai'):
            print("✅ config.settings.ai exists")
            
            # Test custom provider fields
            custom_fields = [
                'custom_provider_enabled',
                'custom_provider_name', 
                'custom_provider_api_key',
                'custom_provider_model',
                'custom_provider_api_url'
            ]
            
            for field in custom_fields:
                if hasattr(config.settings.ai, field):
                    print(f"✅ config.settings.ai.{field} exists")
                else:
                    print(f"❌ config.settings.ai.{field} is missing")
        else:
            print("❌ config.settings.ai is missing")
    else:
        print("❌ config.settings is missing")
        
except Exception as e:
    print(f"❌ Error: {e}")

print("\n=== Test Complete ===")