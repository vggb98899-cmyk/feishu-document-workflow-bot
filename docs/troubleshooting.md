# 故障排查

## 机器人没有回复

1. 检查 Main 与 Supervisor 是否运行。
2. 检查日志是否出现 `LONG_CONNECTION_STARTING` 和 `Lark connected`。
3. 确认飞书应用已订阅 `im.message.receive_v1` 并发布当前版本。
4. 群聊必须先 `@机器人`，单聊可直接发送指令。

## 配置启动失败

确认项目根目录存在 `.env`，且 App ID、App Secret 和 Folder Token 不是占位符。程序会对缺失配置 fail-fast，不会退回根目录创建文档。

## COPY 提示原稿不可访问

确认链接属于 `*.feishu.cn/docx/<token>`，并确认应用身份能够读取原稿。发指令者无需预先成为原稿协作者。

## operation 进入 UNKNOWN 或 AMBIGUOUS

不要重复发送相同业务请求来“试一次”。先根据 SQLite 中保存的 operation、checkpoint 和资源 Token 做只读核验；确认外部副作用未发生后，才能设计人工修复。

## 服务重启后仍无响应

计划任务显示 `Ready` 只表示任务已注册，不代表 Supervisor 正在运行。应核对实际 Python 进程和最新长连接日志。
