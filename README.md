# Prompt Lens

图片 / 视频生成提示词反推工具。当前 MVP 支持图片上传、结构化视觉分析、提示词编辑和多模型格式输出；视频入口已预留，后续接入关键帧与分镜分析。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 9001
```

没有 `OPENAI_API_KEY` 时，接口会返回内置 Demo 分析，方便验证 UI；配置 key 后会调用兼容 Chat Completions 的 Vision API。

## Linux 部署

```bash
sudo mkdir -p /opt/prompt-lens
sudo cp -R . /opt/prompt-lens
cd /opt/prompt-lens
sudo cp .env.example .env
sudo bash deploy/install.sh
```

服务监听 `9001`，健康检查为 `/health`。如果通过 Nginx 代理，请将 `/` 反代到 `127.0.0.1:9001`，并保留上传请求体大小设置。
