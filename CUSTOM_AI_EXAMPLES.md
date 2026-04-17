# 自定义AI提供商使用示例

## 示例配置

### 场景1: 使用本地部署的Ollama
```
1. 在Web面板中:
   - Provider: 选择 "Custom Provider"
   - Custom Provider Name: ollama-local
   - Custom Model Name: llama3:70b
   - Custom API URL: http://localhost:11434/v1/chat/completions
   - API Key: (留空或填写任意值，Ollama通常不需要API密钥)
   - Enable Custom Provider: 勾选

2. 在.env文件中:
   CUSTOM_AI_PROVIDER_ENABLED=true
   CUSTOM_AI_PROVIDER_NAME=ollama-local
   CUSTOM_AI_MODEL=llama3:70b
   CUSTOM_AI_API_URL=http://localhost:11434/v1/chat/completions
   CUSTOM_AI_API_KEY=
   AI_PROVIDER=ollama-local
```

### 场景2: 使用OpenRouter API
```
1. 在Web面板中:
   - Provider: 选择 "Custom Provider"
   - Custom Provider Name: openrouter
   - Custom Model Name: google/gemini-2.0-flash-exp:free
   - Custom API URL: https://openrouter.ai/api/v1/chat/completions
   - API Key: sk-or-v1-xxxxxxxxxxxxxxxxxxxx
   - Enable Custom Provider: 勾选

2. 在.env文件中:
   CUSTOM_AI_PROVIDER_ENABLED=true
   CUSTOM_AI_PROVIDER_NAME=openrouter
   CUSTOM_AI_MODEL=google/gemini-2.0-flash-exp:free
   CUSTOM_AI_API_URL=https://openrouter.ai/api/v1/chat/completions
   CUSTOM_AI_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx
   AI_PROVIDER=openrouter
```

### 场景3: 使用自有AI服务器
```
1. 在Web面板中:
   - Provider: 选择 "Custom Provider"
   - Custom Provider Name: company-ai
   - Custom Model Name: trading-gpt-v1
   - Custom API URL: https://ai.company.com/api/v1/chat/completions
   - API Key: company-ai-token-xxxx
   - Enable Custom Provider: 勾选

2. 在.env文件中:
   CUSTOM_AI_PROVIDER_ENABLED=true
   CUSTOM_AI_PROVIDER_NAME=company-ai
   CUSTOM_AI_MODEL=trading-gpt-v1
   CUSTOM_AI_API_URL=https://ai.company.com/api/v1/chat/completions
   CUSTOM_AI_API_KEY=company-ai-token-xxxx
   AI_PROVIDER=company-ai
```

## 验证步骤

### 步骤1: 检查配置
```bash
cd signal-server
python test_structure.py
```

应该看到所有自定义AI提供商的字段都存在。

### 步骤2: 测试API调用（可选）
```bash
# 先启用自定义提供商
echo "CUSTOM_AI_PROVIDER_ENABLED=true" >> .env
echo "AI_PROVIDER=custom" >> .env

# 重启服务
docker-compose restart
```

### 步骤3: 通过Web面板验证
1. 访问 http://[你的服务器IP]:8000/
2. 进入 Settings 页面
3. 检查AI Provider设置是否正确显示
4. 尝试保存新的自定义提供商配置

## 常见问题解决方案

### Q1: 自定义提供商不生效
**检查点:**
1. `.env` 文件中 `CUSTOM_AI_PROVIDER_ENABLED=true`
2. `AI_PROVIDER` 的值与 `CUSTOM_AI_PROVIDER_NAME` 一致
3. 重启服务使配置生效

### Q2: API调用返回错误
**调试方法:**
1. 查看Docker日志: `docker logs signal-server`
2. 检查API URL是否正确
3. 验证API密钥是否有权限
4. 确认API端点支持OpenAI兼容格式

### Q3: 前端显示不正确
**解决方法:**
1. 清除浏览器缓存
2. 检查JavaScript控制台是否有错误
3. 确保 `static/app.js` 已正确更新

## 性能优化建议

1. **连接池**: 对于高频率请求，考虑配置HTTP连接池
2. **超时设置**: 根据API响应时间调整超时设置
3. **缓存**: 对于重复的查询结果可以考虑缓存
4. **监控**: 添加API调用成功率和响应时间的监控

## 安全性注意事项

1. **API密钥保护**: 确保API密钥不在日志中泄露
2. **HTTPS**: 生产环境始终使用HTTPS
3. **访问控制**: 限制API端点的访问权限
4. **输入验证**: 验证所有API响应的格式

## 扩展功能建议

1. **多提供商支持**: 可以扩展支持多个自定义提供商
2. **负载均衡**: 对于多个API端点实现负载均衡
3. **故障转移**: 当主API不可用时自动切换到备用
4. **性能分析**: 添加不同提供商的性能对比分析