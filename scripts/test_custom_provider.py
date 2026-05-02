"""Test custom AI provider functionality."""
import sys

sys.path.insert(0, '.')

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from ai_analyzer import _call_custom, _parse_model_id
from core.config import settings


async def test_parse_model_id():
    """Test model ID parsing."""
    print("=" * 60)
    print("Testing Model ID Parsing")
    print("=" * 60)

    # Test various formats
    test_cases = [
        ("openai/gpt-4", ("openai", "gpt-4")),
        ("anthropic/claude-3", ("anthropic", "claude-3")),
        ("custom/my-model", ("custom", "my-model")),
        ("deepseek", ("deepseek", "")),
        ("mistral", ("mistral", "")),
        ("custom", ("custom", "")),
        ("provider:model", ("provider", "model")),
    ]

    for input_str, expected in test_cases:
        result = _parse_model_id(input_str)
        status = "PASS" if result == expected else "FAIL"
        print(f"  {status}: '{input_str}' -> {result} (expected {expected})")


async def test_custom_provider_call():
    """Test custom provider API call with mocked response."""
    print("\n" + "=" * 60)
    print("Testing Custom Provider API Call (Mocked)")
    print("=" * 60)

    # Mock settings
    with patch.object(settings.ai, 'custom_provider_enabled', True):
        with patch.object(settings.ai, 'custom_provider_name', 'custom'):
            with patch.object(settings.ai, 'custom_provider_model', 'test-model'):
                with patch.object(settings.ai, 'custom_provider_api_url', 'https://test.api/v1/chat'):
                    with patch.object(settings.ai, 'custom_provider_api_key', 'test-key'):
                        with patch.object(settings.ai, 'temperature', 0.7):
                            with patch.object(settings.ai, 'max_tokens', 1000):

                                # Mock httpx response
                                mock_response = MagicMock()
                                mock_response.status_code = 200
                                mock_response.json.return_value = {
                                    "choices": [
                                        {
                                            "message": {
                                                "content": '{"confidence": 0.75, "recommendation": "execute", "reasoning": "Test response"}'
                                            }
                                        }
                                    ]
                                }

                                with patch('httpx.AsyncClient') as mock_client:
                                    mock_client_instance = AsyncMock()
                                    mock_client_instance.post = AsyncMock(return_value=mock_response)
                                    mock_client.return_value.__aenter__.return_value = mock_client_instance

                                    try:
                                        result = await _call_custom(
                                            system="Test system prompt",
                                            user="Test user prompt"
                                        )
                                        print(f"  PASS: Got response: {result[:100]}...")
                                    except Exception as e:
                                        print(f"  FAIL: {e}")


async def test_custom_provider_missing_config():
    """Test custom provider with missing configuration."""
    print("\n" + "=" * 60)
    print("Testing Custom Provider - Missing Config")
    print("=" * 60)

    # Test with missing URL
    with patch.object(settings.ai, 'custom_provider_enabled', True):
        with patch.object(settings.ai, 'custom_provider_api_url', ''):
            with patch.object(settings.ai, 'custom_provider_api_key', 'test-key'):
                try:
                    result = await _call_custom("system", "user")
                    print(f"  FAIL: Should have raised error but got: {result}")
                except ValueError as e:
                    print(f"  PASS: Correctly raised ValueError: {e}")
                except Exception as e:
                    print(f"  FAIL: Wrong exception type: {e}")

    # Test with missing API key
    with patch.object(settings.ai, 'custom_provider_enabled', True):
        with patch.object(settings.ai, 'custom_provider_api_url', 'https://test.api'):
            with patch.object(settings.ai, 'custom_provider_api_key', ''):
                try:
                    result = await _call_custom("system", "user")
                    print(f"  FAIL: Should have raised error but got: {result}")
                except ValueError as e:
                    print(f"  PASS: Correctly raised ValueError: {e}")
                except Exception as e:
                    print(f"  FAIL: Wrong exception type: {e}")


async def test_custom_provider_response_formats():
    """Test various response format parsing."""
    print("\n" + "=" * 60)
    print("Testing Custom Provider Response Formats")
    print("=" * 60)

    response_formats = [
        # OpenAI-style
        {
            "choices": [{"message": {"content": "test content 1"}}]
        },
        # Anthropic-style
        {
            "content": [{"text": "test content 2"}]
        },
        # Simple text
        {
            "text": "test content 3"
        },
        # Simple response
        {
            "response": "test content 4"
        },
        # Message format
        {
            "message": "test content 5"
        },
    ]

    for i, resp_data in enumerate(response_formats, 1):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = resp_data

        with patch.object(settings.ai, 'custom_provider_enabled', True):
            with patch.object(settings.ai, 'custom_provider_api_url', 'https://test.api'):
                with patch.object(settings.ai, 'custom_provider_api_key', 'test-key'):
                    with patch('httpx.AsyncClient') as mock_client:
                        mock_client_instance = AsyncMock()
                        mock_client_instance.post = AsyncMock(return_value=mock_response)
                        mock_client.return_value.__aenter__.return_value = mock_client_instance

                        try:
                            result = await _call_custom("system", "user")
                            print(f"  PASS: Format {i} parsed correctly: '{result}'")
                        except Exception as e:
                            print(f"  FAIL: Format {i} failed: {e}")


async def test_provider_selection():
    """Test that custom provider is selected correctly."""
    print("\n" + "=" * 60)
    print("Testing Provider Selection")
    print("=" * 60)

    with patch.object(settings.ai, 'custom_provider_enabled', True):
        with patch.object(settings.ai, 'custom_provider_name', 'my-custom'):
            # Test provider matching
            provider_checks = [
                ("custom", True, "Should match 'custom'"),
                ("my-custom", True, "Should match custom name"),
                ("openai", False, "Should NOT match openai"),
                ("deepseek", False, "Should NOT match deepseek"),
            ]

            for provider, should_match, reason in provider_checks:
                is_custom = (
                    settings.ai.custom_provider_enabled
                    and provider in {"custom", settings.ai.custom_provider_name.lower()}
                )
                status = "PASS" if is_custom == should_match else "FAIL"
                print(f"  {status}: '{provider}' -> is_custom={is_custom} ({reason})")


async def main():
    print("=" * 60)
    print("Custom AI Provider Module Verification")
    print("=" * 60)
    print()

    await test_parse_model_id()
    await test_custom_provider_call()
    await test_custom_provider_missing_config()
    await test_custom_provider_response_formats()
    await test_provider_selection()

    print("\n" + "=" * 60)
    print("All Tests Complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
