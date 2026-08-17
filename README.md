# Prompt Lens

图片 / 视频生成提示词反推工具。图片通过 Vision API 分析；视频会提取 6 个带时间戳的关键帧，分析为可编辑分镜；结果持久化到服务器 SQLite。

必须配置 OPENAI_API_KEY 才能分析。未配置时 /health 会标记 configured=false，上传接口返回 503，不会伪造结果。

视频依赖 ffmpeg/ffprobe，OpenCloudOS 可执行 dnf install -y ffmpeg-free。服务监听 9001，健康检查为 /health。若通过 Nginx 代理，请将 / 反代到 127.0.0.1:9001，并将 client_max_body_size 设为至少 180m。
