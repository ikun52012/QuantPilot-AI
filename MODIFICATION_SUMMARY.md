# 自定义AI提供商功能修改说明

## 修改概述
本次修改为Signal Server添加了自定义AI提供商功能，允许用户通过Web面板配置自定义的AI API端点。

## 修改的文件列表

### 1. 核心配置文件
- `config.py` - 添加了自定义AI提供商的配置字段
- `ai_analyzer.py` - 添加了自定义AI提供商的调用逻辑
- `main.py` - 扩展了API端点以支持自定义提供商配置

### 2. 前端文件
- `static/index.html` - 添加了自定义提供商的UI界面
- `static/app.js` - 添加了自定义提供商的前端逻辑

### 3. 配置文件
- `.env.example` - 更新了示例配置
- `.env` - 更新了当前配置

### 4. 文档和测试文件
- `CUSTOM_AI_PROVIDER_GUIDE.md` - 详细使用指南
- `CUSTOM_AI_EXAMPLES.md` - 使用示例
- `test_custom_ai.py` - 功能测试脚本
- `test_config.py` - 配置测试脚本
- `test_structure.py` - 结构测试脚本

## 具体修改内容

### config.py
- 在 `AIConfig` 类中添加了以下字段：
  - `custom_provider_enabled`: 是否启用自定义提供商
  - `custom_provider_name`: 自定义提供商名称
  - `custom_provider_api_key`: 自定义提供商API密钥
  - `custom_provider_model`: 自定义模型名称
  - `custom_provider_api_url`: 自定义API地址

### ai_analyzer.py
- 添加了 `_call_custom` 函数，用于调用自定义AI提供商
- 修改了 `analyze_signal` 函数，添加了对自定义提供商的支持
- 支持OpenAI和Anthropic兼容的API格式

### main.py
- 扩展了 `AISettingsRequest` 模型，添加了自定义提供商字段
- 修改了 `save_ai_settings` API端点，支持保存自定义提供商配置
- 更新了 `api_status` 端点，返回自定义提供商信息

### static/index.html
- 在AI提供商下拉菜单中添加了"Custom Provider"选项
- 添加了自定义提供商的配置表单字段
- 使用 `onchange` 事件控制表单显示/隐藏

### static/app.js
- 添加了 `toggleCustomAIFields` 函数
- 修改了 `saveAISettings` 函数，处理自定义提供商配置
- 更新了 `loadSettings` 函数，正确显示自定义提供商状态

## 功能特性

1. **灵活的配置**: 支持任意OpenAI兼容的API端点
2. **完整的前端集成**: 通过Web面板即可配置
3. **完善的错误处理**: 支持API调用失败时的重试和回退
4. **安全的密钥管理**: API密钥通过环境变量管理
5. **详细的文档**: 提供完整的使用指南和示例

## 使用方法

### 通过Web面板配置
1. 访问 Signal Server Web 面板
2. 进入 Settings → AI Provider & Custom Options
3. 选择 "Custom Provider"
4. 填写自定义提供商信息
5. 点击保存

### 通过环境变量配置
在 `.env` 文件中设置：
```bash
CUSTOM_AI_PROVIDER_ENABLED=true
CUSTOM_AI_PROVIDER_NAME=your-provider-name
CUSTOM_AI_API_KEY=your-api-key
CUSTOM_AI_MODEL=your-model-name
CUSTOM_AI_API_URL=https://your-api-endpoint.com/v1/chat/completions
AI_PROVIDER=your-provider-name
```

## 测试方法

### 单元测试
```bash
cd signal-server
python test_structure.py  # 测试配置结构
python test_config.py     # 测试配置加载
```

### 功能测试
```bash
cd signal-server
python test_custom_ai.py  # 测试自定义AI提供商功能
```

## 注意事项

1. **API兼容性**: 自定义API端点必须支持OpenAI或Anthropic兼容格式
2. **性能影响**: API响应时间会影响交易信号分析速度
3. **错误处理**: 系统提供了重试机制，但仍需确保API稳定性
4. **安全性**: 确保API密钥的安全存储

## 后续优化建议

1. 添加更多API格式支持
2. 实现API健康检查
3. 添加API调用统计
4. 支持多个自定义提供商
5. 添加API响应格式自定义配置