# Codex Reset 监控器

这是一个用于监控 <https://codex-reset.com/> 公开预测数据的个人工具。

GitHub Actions 每 20 分钟从
`https://codex-reset.com/api/forecast` 读取 `probabilities.rounded_24h`。当该值从
`80%` 或以下上升至严格大于 `80%` 时，工作流会通过 Gmail 向自己发送一封中文提醒邮件。在数值重新回到 `80%` 或以下，并再次越过阈值前，不会重复发送概率提醒。

监控器还会保存 `last_reset_at` 作为基线。后续检测到该时间变新时，会发送一封重置提醒邮件。这是对网页上“距上次重置时间”突然回退的可靠判断方式。

## 行为

- 每小时的第 7、27、47 分钟运行一次。
- 首次检测到概率高于 80% 时发送一次提醒。
- 首次记录 `last_reset_at` 时不提醒；之后该值变新时发送一次重置提醒。
- 仅在 `monitor-state` 分支保存非敏感状态。
- 连续三次无法读取预测数据后发送故障提醒。
- 预测数据恢复后发送恢复提醒。
- 每 30 天写入一次心跳状态提交，保持公开仓库活跃。
- 支持手动发送测试邮件，且不会改变正式监控状态。

## GitHub 配置

1. 将本项目推送到一个公开 GitHub 仓库。
2. 打开 **Settings > Secrets and variables > Actions**。
3. 新建仓库密钥 `GMAIL_ADDRESS`，值为发送邮件的 Gmail 地址。
4. 新建仓库密钥 `GMAIL_APP_PASSWORD`，值为 16 位 Gmail 应用专用密码。
5. 打开 **Actions > Codex Reset Monitor > Run workflow**。
6. 选择 `send-test-email` 并运行一次。
7. 确认已收到中文测试邮件。

切勿将 Gmail 地址或应用专用密码写入已提交的文件。此工具不使用 Gmail 登录密码。

## 本地测试

```powershell
python -m unittest discover -s tests -v
```

这些测试完全离线，不会发送邮件，也不会调用 GitHub。

## 状态分支

首次定时运行或手动以 `check-now` 运行时，程序会从默认分支创建 `monitor-state`，并写入 `monitor-state.json`。该文件仅包含：

- 监控器是否已初始化；
- 概率是否当前高于阈值；
- 最近一次观察到的全局重置时间；
- 连续读取预测数据失败的次数；
- 是否已发送故障提醒；
- 上次心跳时间。

## 安全

- Gmail 凭据仅从 GitHub Secrets 读取。
- 工作流仅使用内置的、具有 `contents: write` 权限的 `GITHUB_TOKEN` 维护状态分支。
- 拉取请求不会获得仓库密钥。
- 邮件通过 Gmail SMTP 和 STARTTLS 发送。
