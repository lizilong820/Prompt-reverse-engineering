# Prompt Lens 微信小程序

图片/视频提示词反推商业应用，包含原生微信小程序和 FastAPI 后端。

## 商业闭环

- 微信 wx.login 登录，服务端使用 jscode2session 换取 openid
- 新用户赠送积分
- 图片、视频按不同积分价格提交
- 扣费和创建任务在同一数据库事务中完成
- 任务失败自动退款
- idempotency_key 防止重复点击重复扣费
- 激励广告领取使用一次性服务端凭证、每日上限和冷却时间
- SQLite 积分账本记录每次余额变化
- 视频自动提取 6 个关键帧并生成时间轴分镜

## 目录

- miniprogram/：原生微信小程序，在微信开发者工具中导入
- app/：FastAPI 后端
- deploy/：systemd 部署文件

## 必填配置

服务器 /opt/prompt-lens/.env 需要配置 OPENAI_API_KEY、WX_APP_ID、WX_APP_SECRET。

小程序 miniprogram/config.js 需要配置已备案 HTTPS API 域名和激励视频 AD_UNIT_ID。project.config.json 的 appid 需要替换为真实小程序 AppID。

## 正式发布前

1. 域名完成 ICP 备案并配置 HTTPS。
2. 在微信公众平台加入 request 和 uploadFile 合法域名。
3. 在流量主后台创建激励视频广告位。
4. 配置 OpenAI 或兼容 Vision API。
5. 隐私保护指引声明媒体用途、处理方式和保留期限。
6. 关闭开发者工具的“不校验合法域名”后完成真机测试。

激励视频的 isEnded 结果来自微信客户端。服务端已有一次性凭证、防重放、每日上限和冷却时间，但仍应结合设备、IP、行为频率和异常账户封禁加强风控。

## 管理后台

管理地址：`/admin`

生成管理员密码哈希：

```bash
cd /opt/prompt-lens
.venv/bin/python -m app.admin_password
```

把输出写入 `/opt/prompt-lens/.env`：

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=生成的哈希
```

然后重启服务：

```bash
systemctl restart prompt-lens.service
```

后台支持总览、用户搜索、积分调整、封禁/解封、积分流水、任务状态和手动退款。管理员接口使用独立的 PBKDF2 密码哈希和短期 session token。
