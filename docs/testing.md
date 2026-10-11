# 测试说明

## 执行命令

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
```

## 当前结果

- 测试数量：124
- 通过：124
- 失败：0
- 跳过：0
- 真实飞书写入：0

## 主要覆盖范围

- `.env` 缺失和凭证占位符 fail-fast
- 群聊 mention、单聊和富文本链接解析
- 飞书 docx URL 白名单与 Token 提取
- CREATE/COPY Gate 隔离
- 同一 `message_id` 不重复执行
- 不同 `message_id` 独立执行
- UNKNOWN/AMBIGUOUS 不自动重试
- PARTIAL 只修复已知资源
- Owner Transfer 每个 operation 最多一次
- Worker 崩溃检测、恢复扫描和恢复通知幂等
- Main 单实例锁与 Supervisor 行为
- Secret 不进入日志

## 未验证项

公开版本不包含生产凭证，因此没有在发布目录执行真实飞书 CREATE、COPY 或 Owner Transfer。真实环境验证必须由仓库使用者使用自己的飞书应用完成。
