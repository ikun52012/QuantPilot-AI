# 自定义AI提供商功能指南

## 功能概述

Signal Server 现在支持自定义AI提供商功能，允许用户配置自己的AI API端点，包括：
1. 自定义提供商选项（在面板中选择"Custom Provider"）
2. 自定义模型名称
3. 自定义API地址

## 配置步骤

### 1. 环境变量配置

在 `.env` 文件中添加以下配置：

```bash
# 启用自定义AI提供商
CUSTOM_AI_PROVIDER_ENABLED=true

# 自定义提供商名称（将作为AI_PROVIDER的值）
CUSTOM_AI_PROVIDER_NAME=my-custom-llm

# 自定义提供商API密钥
CUSTOM_AI_API_KEY=your-api-key-here

# 自定义模型名称
CUSTOM_AI_MODEL=gpt-3.5-turbo

# 自定义API端点URL
CUSTOM_AI_API_URL=https://api.example.com/v1/chat/completions

# 设置AI提供商为自定义提供商
AI_PROVIDER=my-custom-llm
```

### 2. 通过Web面板配置

1. 访问 Signal Server 的 Web 面板
2. 进入 Settings → AI Provider & Custom Options
3. 在 Provider 下拉菜单中选择 "Custom Provider"
4. 填写以下信息：
   - **Custom Provider Name**: 自定义提供商名称（如：my-llm）
   - **Custom Model Name**: 模型名称（如：gpt-3.5-turbo）
   - **Custom API URL**: API端点地址
   - **API Key**: 对应的API密钥
   - **Enable Custom Provider**: 勾选启用
5. 点击 "Save AI Settings" 保存配置

## 支持的API格式

自定义AI提供商需要支持以下API格式之一：

### 1. OpenAI兼容格式（推荐）
```json
{
  "model": "gpt-3.5-turbo",
  "messages": [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": "用户提示"}
  ],
  "temperature": 0.3,
  "max_tokens": 1000
}
```

响应格式：
```json
{
  "choices": [
    {
      "message": {
        "content": "AI响应内容"
      }
    }
  ]
}
```

### 2. Anthropic兼容格式
```json
{
  "model": "claude-sonnet-4-20250514",
  "messages": [
    {"role": "user", "content": "用户提示"}
  ],
  "system": "系统提示",
  "max_tokens": 1000,
  "temperature": 0.3
}
```

响应格式：
```json
{
  "content": [
    {
      "text": "AI响应内容"
    }
  ]
}
```

## 使用示例

### 示例1: 使用本地部署的LLM
```bash
# .env 配置
CUSTOM_AI_PROVIDER_ENABLED=true
CUSTOM_AI_PROVIDER_NAME=local-llm
CUSTOM_AI_API_KEY=sk-local
CUSTOM_AI_MODEL=llama-3-70b
CUSTOM_AI_API_URL=http://localhost:8080/v1/chat/completions
AI_PROVIDER=local-llm
```

### 示例2: 使用第三方AI服务
```bash
# .env 配置
CUSTOM_AI_PROVIDER_ENABLED=true
CUSTOM_AI_PROVIDER_NAME=third-party
CUSTOM_AI_API_KEY=sk-xxxx
CUSTOM_AI_MODEL=gpt-4o-mini
CUSTOM_AI_API_URL=https://api.thirdparty.com/v1/chat/completions
AI_PROVIDER=third-party
```

## 测试功能

### 方法1: 使用测试脚本
```bash
cd signal-server
python test_custom_ai.py
```

### 方法2: 通过Web面板测试
1. 配置完成后，访问 Web 面板
2. 触发一个交易信号
3. 查看日志确认AI分析是否使用自定义提供商

## 故障排除

### 问题1: "Unknown AI provider: custom"
**原因**: 自定义提供商未正确启用或配置
**解决方法**:
1. 检查 `.env` 文件中 `CUSTOM_AI_PROVIDER_ENABLED=true`
2. 确保 `AI_PROVIDER` 的值与 `CUSTOM_AI_PROVIDER_NAME` 一致
3. 重启服务使配置生效

### 问题2: API调用失败
**原因**: API端点不可达或配置错误
**解决方法**:
1. 检查API URL是否正确
2. 验证API密钥是否有效
3. 确认API端点支持OpenAI兼容格式
4. 查看服务日志获取详细错误信息

### 问题3: 响应格式解析错误
**原因**: API响应格式不符合预期
**解决方法**:
1. 确保API返回OpenAI或Anthropic兼容格式
2. 检查响应中是否包含正确的字段
3. 可以修改 `ai_analyzer.py` 中的 `_call_custom` 函数以适应特定格式

## 代码修改说明

本次修改涉及以下文件：

### 1. config.py
- 添加了自定义AI提供商的配置字段
- 包括：`custom_provider_enabled`, `custom_provider_name`, `custom_provider_api_key`, `custom_provider_model`, `custom_provider_api_url`

### 2. ai_analyzer.py
- 添加了 `_call_custom` 函数用于调用自定义AI提供商
- 修改了 `analyze_signal` 函数以支持自定义提供商
- 支持OpenAI和Anthropic兼容的API格式

### 3. main.py
- 扩展了 `AISettingsRequest` 模型以包含自定义提供商字段
- 修改了 `save_ai_settings` API端点以保存自定义提供商配置
- 更新了 `api_status` 端点以返回自定义提供商信息

### 4. index.html
- 在AI提供商下拉菜单中添加了"Custom Provider"选项
- 添加了自定义提供商的配置表单字段
- 使用JavaScript控制表单的显示/隐藏

### 5. app.js
- 添加了 `toggleCustomAIFields` 函数
- 修改了 `saveAISettings` 函数以处理自定义提供商配置
- 更新了 `loadSettings` 函数以正确显示自定义提供商状态

### 6. .env.example
- 添加了自定义AI提供商的配置示例
- 更新了注释说明

### 7. test_custom_ai.py
- 创建了测试脚本用于验证自定义AI提供商功能

## 注意事项

1. **安全性**: API密钥存储在环境变量中，不会在Web面板中显示
2. **兼容性**: 自定义API端点必须支持OpenAI或Anthropic兼容的格式
3. **性能**: 自定义API端点的响应时间会影响交易信号的分析速度
4. **错误处理**: 系统提供了完善的错误处理和重试机制
5. **回退机制**: 如果自定义API调用失败，系统会使用回退分析

## 后续优化建议

1. 添加更多API格式支持（如Cohere、Google Gemini等）
2. 实现API端点的健康检查
3. 添加API调用统计和监控
4. 支持多个自定义提供商的快速切换
5. 添加API响应格式的自定义配置