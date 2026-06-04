# Local AI Deployment

> **Mac Mini 本地 AI 全栈部署：本地对话 · 文生图 · 手机远程访问，一站式自托管** — A self-hosted local AI stack on Mac Mini: local LLM chat · text-to-image · mobile remote access.

![status](https://img.shields.io/badge/status-active-success) ![version](https://img.shields.io/badge/version-v1.0.0-blue) ![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB) ![platform](https://img.shields.io/badge/platform-macOS-lightgrey) ![license](https://img.shields.io/badge/license-MIT-green)

---

## 概述

**Local_AI_Deployment** 是一套在 Mac Mini（M4 Pro）上落地的本地 AI 全栈方案：文本大模型与文生图全部跑在本机，用一个轻量 Python 桥接服务统一成 OpenAI 兼容接口，再配一个单文件网页聊天界面；借助 Tailscale，手机在任意网络下都能安全访问。**数据不出本机，隐私自己掌控，无需云服务与订阅。**

- **本地文本对话** — 由 LM Studio 提供本地大模型推理（推荐 Qwen 3.6-35B-A3B 日常主力，速度优先可换 Gemma 4），对外暴露 OpenAI 兼容 API（端口 `1234`）。
- **本地文生图** — 由 Draw Things 出图（推荐 Z Image Turbo 极速出图，质量优先可用 FLUX.2 Dev），开启 HTTP API 后服务于端口 `7860`。
- **智能路由桥接** — `bridge.py`（端口 `8000`）自动识别消息中的画图关键词（如「画」「生成」/ `draw`）：命中则把描述翻译成英文 prompt 交给 Draw Things，否则转发给 LM Studio 对话——前端只需对接这一个地址。
- **网页聊天界面** — `chat.html` 单文件前端，深色主题、多会话、模型切换、移动端适配，由桥接服务直接托管。
- **远程访问** — 通过 Tailscale 组网，手机在外网也能安全连回家中的 Mac Mini。

## 系统要求

| 项目 | 要求 |
|---|---|
| 硬件 | Apple Silicon Mac（推荐 Mac Mini M4 Pro）；统一内存 ≥ 32GB，48GB 可跑 35B 级量化模型 |
| 系统 | macOS |
| 运行时 | Python 3.10+（桥接服务仅用标准库，无需 `pip install`）|
| 依赖应用 | [LM Studio](https://lmstudio.ai)（文本）、[Draw Things](https://apps.apple.com/app/draw-things/id6444050820)（文生图）|
| 远程访问 | [Tailscale](https://tailscale.com)（可选）|

## 快速开始

> 完整步骤（含每一步截图与排错）见 **[本地AI全栈搭建教程.md](本地AI全栈搭建教程.md)**。

1. **克隆仓库**
   ```bash
   git clone https://github.com/coderscsy/coderscsy-Local_AI_Deployment.git
   cd coderscsy-Local_AI_Deployment
   ```
2. **LM Studio**：下载并加载模型（推荐 Qwen 3.6-35B-A3B），启动本地服务器，端口 `1234`。
3. **Draw Things**：下载模型（推荐 Z Image Turbo），在设置中开启 HTTP API，端口 `7860`。
4. **启动桥接服务**
   ```bash
   python3 bridge.py
   ```
   默认监听 `8000`，同时托管 `chat.html`。
5. **访问**
   - 电脑：浏览器打开 `http://localhost:8000`
   - 手机：经 Tailscale 打开 `http://<Mac 的 Tailscale IP>:8000`

## 界面预览

| 网页聊天界面 | 对话中直接出图 |
|---|---|
| ![网页聊天界面](images/image16.png) | ![生成示例](images/image14.png) |

## 文件结构

```text
Local_AI_Deployment/
├── bridge.py                   # 桥接 / 路由服务（LM Studio + Draw Things）
├── chat.html                   # 网页聊天界面（由 bridge 托管）
├── 本地AI全栈搭建教程.md         # 完整图文教程（可在 GitHub 直接阅读）
├── images/                     # 教程配图
├── LICENSE
└── README.md
```

## 配置

`bridge.py` 顶部的可调参数：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LM_STUDIO_HOST` | `http://localhost:1234` | LM Studio 服务地址 |
| `DRAW_THINGS_HOST` | `http://localhost:7860` | Draw Things 服务地址 |
| `BRIDGE_PORT` | `8000` | 桥接服务端口（绑定 `0.0.0.0`，供局域网 / Tailscale 访问）|
| `TIMEOUT` | `180` | 请求超时（秒）|
| `DEFAULT_STEPS` | `8` | 默认采样步数 |
| `DEFAULT_WIDTH` / `DEFAULT_HEIGHT` | `1024` | 默认出图尺寸 |
| `IMAGE_DIR` | `~/Pictures/ai-generated` | 生成图片保存目录 |

> 出图参数按 **Z Image Turbo** 调好（`guidance_scale=1`、`guidance_embed=3.5`、`steps=8`）。换 FLUX.2 Dev 等模型时需相应调整 `generate_image()` 里的参数。

## 使用方式

桥接服务对外是标准 OpenAI 兼容接口，会自动判断这条消息是「对话」还是「画图」：

- **普通消息** → 原样转发 LM Studio，正常对话（支持流式）。
- **含画图关键词**（中文「画 / 生成 / 出图 / 生图…」或英文 `draw` / `generate an image`…）→ 自动把描述**翻译成英文** → 交给 Draw Things 出图 → 返回图片链接。

| 你发送 | 行为 |
|---|---|
| `你好，介绍一下你自己` | 文字对话 |
| `画一只戴墨镜的柴犬` | 出图（默认 8 步、1024×1024）|
| `画一只猫 步数30` | 自定义步数（更精细但更慢）|
| `画一只猫 宽1920 高1080` | 自定义尺寸 |

生成的图片保存在 `~/Pictures/ai-generated/`，并可通过 `http://<IP>:8000/images/<文件名>` 访问。

## API

桥接服务暴露 OpenAI 兼容接口，**无需 API Key**。可直接配到 OpenCat 等客户端（Base URL 填 `http://<IP>:8000/v1`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/models` | 列出 LM Studio 已加载的模型 |
| `POST` | `/v1/chat/completions` | 对话 / 画图（OpenAI 兼容，支持 SSE 流式）|
| `GET` | `/images/<name>` | 获取生成的图片 |
| `GET` | `/` | 网页聊天界面 |

## 远程访问

用 [Tailscale](https://tailscale.com) 把 Mac 和手机组进同一虚拟网络后，手机在任意网络（家里 Wi-Fi 或外面 5G）下访问 `http://<Mac 的 Tailscale IP>:8000` 即可。全程加密，免费版支持 100 台设备。详见教程「四、远程访问」。

## 技术栈

`Python`（标准库 `http.server`）· `HTML / CSS / JavaScript` · `LM Studio` · `Draw Things` · `Tailscale`

## 常见问题

| 问题 | 解决方案 |
|---|---|
| 端口被占用 | `lsof -i :8000` 查看占用；避免用 `5000`（被 macOS AirPlay 占用）|
| 一直"思考"无响应 | 确认 LM Studio 与 Draw Things 都已加载模型并开启 API |
| 画图和描述不符 | 中文需先翻译成英文（桥接已自动处理）；换图片模型记得同步调整出图参数 |
| 手机连不上 | 关掉 Mac 上的 VPN 或路由器 AP 隔离，推荐直接用 Tailscale |

更多排错见教程「九、踩坑记录与排查」。

## 致谢

感谢以下开源 / 免费项目：[LM Studio](https://lmstudio.ai) · [Draw Things](https://drawthings.ai) · [Tailscale](https://tailscale.com) · Qwen · Gemma。

## License

本项目采用 [MIT](LICENSE) 许可证开源。
