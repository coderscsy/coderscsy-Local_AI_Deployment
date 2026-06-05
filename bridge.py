"""
LM Studio + Draw Things Bridge
================================
启动: python3 bridge.py
网页: http://<IP>:8000/
API:  http://<IP>:8000/v1
"""

import json
import base64
import time
import os
import re
import socket
import subprocess
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime

LM_STUDIO_HOST = "http://localhost:1234"
DRAW_THINGS_HOST = "http://localhost:7860"
BRIDGE_PORT = 8000
TIMEOUT = 180

IMAGE_DIR = os.path.expanduser("~/Pictures/ai-generated")
os.makedirs(IMAGE_DIR, exist_ok=True)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 中文画图意图：必须出现在消息「开头」（可带「请/帮我/给我」等礼貌前缀）。
# 不再用「画」「生成」这类裸字做全文子串匹配——否则
# “帮我优化提示词…画面…图片生成…”“这幅画谁画的”“讲讲生成式模型”“画质太差” 都会误触发。
# 规则：画/绘 必须后接量词（画一只/画张/画个）；生成/做/出 等通用动词必须后接
# 图像名词（生成…图片）或「张/幅/只」这类偏图像/动物量词（生成一张/生成一只）。
_POLITE   = r'(?:(?:请|帮我|帮忙|帮|给我|替我|麻烦你?|能不能|能否|可不可以|可以)\s*)*'
_QTY      = r'(?:一\s*)?[张幅个只条头份副组片張]'        # 画/绘 用的广义量词
_IMG_QTY  = r'(?:一\s*)?[张幅只副張]'                    # 生成类用的偏图像/动物量词
_IMG_NOUN = r'(?:图|图片|照片|图像|画像|插画|插图|海报|头像|壁纸|表情包?|logo|图标|封面|贴纸|二次元)'
_GEN_VERB = r'(?:生成|制作|做|生|出|搞|整|来)'

ZH_IMAGE_RE = re.compile(
    r'^[\s，。、：:!！?？.…—\-]*' + _POLITE + r'(?:'
    r'(?:画出|手绘|绘制)'                                       # 强画图动词，可不带量词
    r'|[画绘]' + _QTY +                                         # 画/绘 + 量词：画一只 / 画张 / 画个
    r'|' + _GEN_VERB + _IMG_QTY +                               # 生成一只 / 做一张 / 来一幅
    r'|' + _GEN_VERB + r'[^。.!?！？\n]{0,12}?' + _IMG_NOUN +   # 生成…图片 / 做个海报
    r')',
    re.IGNORECASE,
)

# 用于从开头剥掉画图指令，得到真正的画面描述
ZH_PREFIX_RE = re.compile(
    r'^[\s，。、：:!！?？.…—\-]*' + _POLITE +
    r'(?:画出|手绘|绘制|[画绘]' + _QTY + r'?|' + _GEN_VERB + _QTY + r'?)\s*',
    re.IGNORECASE,
)

# 英文触发：视觉动词(draw/sketch/paint…)单独即可；通用动词(generate/create/make…)
# 必须后接"图像类名词"才触发，避免 "generate a list" / "create a function" 误触发。
# 用 \b 词边界，避免 "withdraw" / "drawing" 里的子串误匹配。
EN_IMAGE_RE = re.compile(
    r'\b(?:draw|sketch|paint|illustrate|doodle)\b'
    r'|\b(?:generate|create|make|produce|render)\b[^.?!\n]{0,40}?'
    r'\b(?:image|images|picture|pictures|pic|photo|drawing|painting|illustration|art|artwork|wallpaper|portrait|logo|icon|sticker)\b'
    r'|\b(?:image|picture|photo)\b\s+of\b'
    r'|(?:^|\s)/(?:draw|image)\b',
    re.IGNORECASE,
)

DEFAULT_STEPS = 8
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
EXTERNAL_HOST = None


def unload_all_models():
    """切换模型时先卸载所有已加载模型，腾出内存（依赖 LM Studio 的 lms CLI；需先 `lms bootstrap` 安装到 PATH）。
    搭配已开启的 JIT：卸载后下一次请求会自动加载目标模型，从而实现"先卸当前→再载新的"。"""
    try:
        r = subprocess.run(["lms", "unload", "--all"], capture_output=True, text=True, timeout=30)
        print(f"  [SWITCH] lms unload --all → rc={r.returncode}")
        return True
    except FileNotFoundError:
        print("  [SWITCH] 未找到 lms 命令：请在 Mac 执行 `lms bootstrap` 安装后重启 bridge（已跳过自动卸载）")
        return False
    except Exception as ex:
        print(f"  [SWITCH] 卸载出错：{ex}")
        return False


LOADED_MODEL = None
LOADED_CTX = None

def ensure_loaded(model, ctx):
    """网页端指定上下文长度时：卸载旧模型，再用 `lms load --context-length` 显式加载该模型，
    使上下文长度可在前端调节（依赖 lms CLI；缺失/失败则优雅跳过，回退到 JIT/护栏）。"""
    global LOADED_MODEL, LOADED_CTX
    if not model or not ctx:
        return
    if model == LOADED_MODEL and ctx == LOADED_CTX:
        return                              # 已按该(模型,上下文)加载，免重复重载
    try:
        subprocess.run(["lms", "unload", "--all"], capture_output=True, text=True, timeout=30)
        r = subprocess.run(["lms", "load", model, "--context-length", str(ctx), "-y"],
                           capture_output=True, text=True, timeout=300)
        print(f"  [CTX] lms load {model} --context-length {ctx} → rc={r.returncode} {(r.stderr or '').strip()[:160]}")
        if r.returncode == 0:
            LOADED_MODEL, LOADED_CTX = model, ctx
    except FileNotFoundError:
        print("  [CTX] 未找到 lms（请先在 Mac 执行 `lms bootstrap`）——上下文调节暂不生效")
    except Exception as e:
        print(f"  [CTX] 加载出错：{e}")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def call_api(url, data=None):
    headers = {"Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [ERROR] {url} - {e}")
        return None


def extract_user_message(messages):
    for msg in reversed(messages):
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if isinstance(c, str):
                return c
            elif isinstance(c, list):
                return " ".join(
                    i.get("text", "") if isinstance(i, dict) else str(i) for i in c
                )
    return ""


def has_image_input(messages):
    """最近一条用户消息是否带上传图片（视觉请求）——若是则不走画图、直接转发给视觉模型。"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            c = msg.get("content")
            return isinstance(c, list) and any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in c)
    return False


def is_image_request(text):
    low = text.lower()
    if "create a simple and clear title" in low or "generate a title" in low:
        return False
    hit = bool(ZH_IMAGE_RE.search(text)) or bool(EN_IMAGE_RE.search(text))
    if not hit:
        return False
    # 只发了指令、没有实际描述（如"画一只"/"生成"/"draw"）-> 不触发生图，走普通对话
    return bool(extract_description(text))


def parse_params(text):
    p = {"steps": DEFAULT_STEPS, "width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT}
    # 否定后顾 (?<!汉字)：避免把"身高143""提高100""加宽"等词里的 高/宽 误当成图片尺寸（保留"猫 宽1920 高1080"这种）
    # 仅在合理范围内才采用，否则用默认值——避免"高1"这类误匹配产出 1px 的非法尺寸让 Draw Things 422
    m = re.search(r'(?:(?<![一-鿿])步数|\bsteps?)\s*(\d+)', text, re.IGNORECASE)
    if m and 1 <= int(m.group(1)) <= 150: p["steps"] = int(m.group(1))
    m = re.search(r'(?:(?<![一-鿿])宽|\bwidth)\s*(\d+)', text, re.IGNORECASE)
    if m and 64 <= int(m.group(1)) <= 4096: p["width"] = int(m.group(1)) // 8 * 8   # 对齐到 8 的倍数
    m = re.search(r'(?:(?<![一-鿿])高|\bheight)\s*(\d+)', text, re.IGNORECASE)
    if m and 64 <= int(m.group(1)) <= 4096: p["height"] = int(m.group(1)) // 8 * 8
    return p


def extract_description(text):
    s = text
    # 去掉尺寸/步数参数
    s = re.sub(r'(?:(?<![一-鿿])步数|\bsteps?)\s*\d+', '', s, flags=re.IGNORECASE)
    s = re.sub(r'(?:(?<![一-鿿])宽|\bwidth)\s*\d+', '', s, flags=re.IGNORECASE)
    s = re.sub(r'(?:(?<![一-鿿])高|\bheight)\s*\d+', '', s, flags=re.IGNORECASE)
    # 去掉斜杠命令前缀 /draw /image /img /gen
    s = re.sub(r'^\s*/(?:draw|image|img|gen|generate)\b\s*', '', s, flags=re.IGNORECASE)
    # 去掉英文触发短语前缀：(please/can you…) draw/create/make… (a/an/the/me/some) (image/picture…) (of)
    s = re.sub(
        r'^\s*(?:(?:please|can\s+you|could\s+you|pls|help\s+me|hey|hi)\s+)*'
        r'(?:draw|sketch|paint|illustrate|doodle|generate|create|make|produce|render|give\s+me|show\s+me)\b\s*'
        r'(?:(?:an?|the|me|some)\s+)*'
        r'(?:(?:image|images|picture|pictures|pic|photo|drawing|painting|illustration|art|artwork|wallpaper|portrait|logo|icon|sticker)\s*)?'
        r'(?:of\s+)?',
        '', s, count=1, flags=re.IGNORECASE,
    )
    # 去掉开头的中文画图指令（画一只 / 帮我画个 / 生成一张…），得到真正的描述
    s = ZH_PREFIX_RE.sub('', s, count=1)
    return s.strip("，,。.：:!！?？ \n\t")


def translate_to_english(text, model):
    if not re.search(r'[\u4e00-\u9fff]', text):
        return text
    data = {
        "model": model, "stream": False, "temperature": 0.2, "max_tokens": 4096,
        "messages": [
            {"role": "system", "content":
                "Translate the following Chinese text into English. "
                "Translate faithfully and completely. Keep every detail. "
                "Do NOT add, remove or change anything. "
                "Output ONLY the English translation, nothing else."},
            {"role": "user", "content": text}
        ]
    }
    result = call_api(f"{LM_STUDIO_HOST}/v1/chat/completions", data)
    if result and "choices" in result:
        t = result["choices"][0]["message"]["content"]
        # 去掉推理模型的 <think> 思考——否则中文思考会混进提示词、看起来像"翻译成了中文"
        t = re.sub(r'<think>[\s\S]*?</think>', '', t, flags=re.IGNORECASE)
        t = re.sub(r'<think>[\s\S]*$', '', t, flags=re.IGNORECASE)   # 未闭合的也去掉
        t = t.strip()
        if t:
            return t
    return text


def generate_image(prompt, params):
    data = {
        "prompt": prompt,
        "negative_prompt": "blurry, low quality, distorted, deformed",
        "width": params["width"], "height": params["height"],
        "steps": params["steps"],
        "guidance_scale": 1, "guidance_embed": 3.5, "seed": -1
    }
    print(f"  [DRAW] {params['steps']}步 {params['width']}x{params['height']}")
    print(f"  [DRAW] prompt = {prompt}")
    result = call_api(f"{DRAW_THINGS_HOST}/sdapi/v1/txt2img", data)
    if result and "images" in result and result["images"]:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"img_{ts}.png"
        fpath = os.path.join(IMAGE_DIR, fname)
        img_bytes = base64.b64decode(result["images"][0])
        with open(fpath, "wb") as f:
            f.write(img_bytes)
        print(f"  [DRAW] ✅ {fpath} ({len(img_bytes)//1024}KB)")
        return fpath, fname
    print("  [DRAW] ❌ Failed")
    return None, None


class BridgeHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")

    def _send(self, data, status=200, ct="application/json"):
        body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else \
               (data if isinstance(data, bytes) else data.encode())
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self, model):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        ts = int(time.time())
        cid = f"chatcmpl-{ts}"
        self._sse = {"ts": ts, "cid": cid, "model": model}
        self._sse_chunk({"role": "assistant"})

    def _sse_text(self, text):
        for i in range(0, len(text), 100):
            self._sse_chunk({"content": text[i:i+100]})

    def _sse_chunk(self, delta):
        s = self._sse
        obj = {"id": s["cid"], "object": "chat.completion.chunk", "created": s["ts"],
               "model": s["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def _sse_end(self):
        s = self._sse
        end = {"id": s["cid"], "object": "chat.completion.chunk", "created": s["ts"],
               "model": s["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(f"data: {json.dumps(end)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_HEAD(self):
        if self.path.startswith("/images/"):
            fname = self.path.split("/images/")[-1]
            fpath = os.path.join(IMAGE_DIR, fname)
            if os.path.exists(fpath):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(os.path.getsize(fpath)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path == "/v1/models":
            r = call_api(f"{LM_STUDIO_HOST}/v1/models")
            self._send(r if r else {"error": "Failed"})

        elif self.path.startswith("/images/"):
            fname = self.path.split("/images/")[-1]
            fpath = os.path.join(IMAGE_DIR, fname)
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                # 生成的图片不会变：让浏览器长期缓存，切换/回看对话时不再重复下载（之前每次切换都重新拉，故觉得慢）
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send({"error": "Not found"}, 404)

        elif self.path in ('/', '/chat', '/chat.html'):
            hp = os.path.join(SCRIPT_DIR, 'chat.html')
            if os.path.exists(hp):
                with open(hp, 'rb') as f:
                    self._send(f.read(), ct='text/html; charset=utf-8')
            else:
                self._send({"error": "chat.html not found"}, 404)

        else:
            self._send({"service": "Bridge"})

    def do_POST(self):
        global EXTERNAL_HOST

        if self.path != "/v1/chat/completions":
            self._send({"error": f"Unknown: {self.path}"}, 404)
            return

        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            body = json.loads(raw.decode("utf-8"))
        except:
            self._send({"error": "Bad JSON"}, 400)
            return

        messages = body.get("messages", [])
        model = body.get("model", "qwen/qwen3.6-35b-a3b")
        ctx_len = body.pop("context_length", None)   # 网页端选择的上下文长度（自定义字段，转发前移除）
        if ctx_len:
            ensure_loaded(model, ctx_len)            # 卸载并用指定 context-length 重新加载，使上下文可调
        is_stream = body.get("stream", False)
        last_msg = extract_user_message(messages)

        h = self.headers.get("Host", "")
        if h and "100." in h:
            EXTERNAL_HOST = h

        print(f"\n  [IN] stream={is_stream}")
        print(f"  [IN] msg = {last_msg}")

        if is_image_request(last_msg) and not has_image_input(messages):   # 带上传图片=视觉请求，不画图，转发给模型
            print(f"  [BRIDGE] 🎨 Image!")

            params = parse_params(last_msg)
            description = extract_description(last_msg)
            print(f"  [BRIDGE] desc = {description}")

            if is_stream:
                self._sse_start(model)
                self._sse_text("🎨 正在生成图片，请稍候…\n\n")

                prompt = translate_to_english(description, model)
                print(f"  [BRIDGE] prompt = {prompt}")
                # 用代码块包住提示词 → 网页端自带"复制"按钮，可一键复制
                self._sse_text(f"```提示词\n{prompt}\n```\n\n")

                fpath, fname = generate_image(prompt, params)

                if fpath and fname:
                    host = EXTERNAL_HOST or self.headers.get("Host", f"localhost:{BRIDGE_PORT}")
                    url = f"http://{host}/images/{fname}"
                    self._sse_text(
                        f"✅ 生成完成 · {params['steps']} 步 · {params['width']}×{params['height']}\n\n"
                        f"{url}\n\n"
                        f"路径: {fpath}"
                    )
                else:
                    self._sse_text("\n❌ 生成失败，请检查 Draw Things")

                self._sse_end()

            else:
                prompt = translate_to_english(description, model)
                print(f"  [BRIDGE] prompt = {prompt}")
                fpath, fname = generate_image(prompt, params)

                if fpath and fname:
                    host = EXTERNAL_HOST or self.headers.get("Host", f"localhost:{BRIDGE_PORT}")
                    url = f"http://{host}/images/{fname}"
                    text = (f"🎨 图片已生成\n\n```提示词\n{prompt}\n```\n\n"
                            f"{params['steps']} 步 · {params['width']}×{params['height']}\n\n{url}")
                else:
                    text = "❌ 生成失败"

                self._send({
                    "id": f"chatcmpl-{int(time.time())}", "object": "chat.completion",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                })

        else:
            print(f"  [BRIDGE] 💬 -> LM Studio")
            headers_sent = False
            def _open():
                req = urllib.request.Request(
                    f"{LM_STUDIO_HOST}/v1/chat/completions",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"}, method="POST"
                )
                return urllib.request.urlopen(req, timeout=TIMEOUT)
            try:
                try:
                    resp = _open()
                except Exception as oe:
                    d = oe.read().decode("utf-8", "replace") if hasattr(oe, "read") else ""
                    # 切换模型常见的"加载失败/资源不足"400：卸载旧模型后重试一次（先卸当前→再载新的）
                    if getattr(oe, "code", None) == 400 and re.search(r"failed to load|insufficient|resource|out of memory|加载失败|资源|内存", d, re.I) and unload_all_models():
                        print("  [SWITCH] 资源不足/加载失败：已卸载旧模型，重试加载新模型…")
                        time.sleep(0.5)
                        resp = _open()
                    else:
                        oe._detail = d           # 不可重试：把已读到的报错带给外层透传，避免二次 read 读空
                        raise
                ct = resp.headers.get("Content-Type", "application/json")
                is_stream = "event-stream" in ct
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Access-Control-Allow-Origin", "*")
                # 关键：流结束就关连接（不要 keep-alive），客户端据此退出"生成中"状态、才能继续输入
                self.send_header("Connection", "close")
                if is_stream:
                    self.send_header("Cache-Control", "no-cache")
                else:
                    cl = resp.headers.get("Content-Length")
                    if cl:
                        self.send_header("Content-Length", cl)
                self.end_headers()
                headers_sent = True

                # 按行读取（SSE 是行分隔）：readline 一遇到 \n 就返回，能即时透传、并即时发现 [DONE]，
                # 不像 read(4096) 会一直阻塞到凑满缓冲或连接关闭（keep-alive 时永远等不到 → 卡死）
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
                    if is_stream and line.strip() == b"data: [DONE]":   # 结束标记：立即收尾，不再死等连接关闭
                        break
                resp.close()
            except Exception as e:
                code = getattr(e, "code", None)        # HTTPError 自带 .code/.read()，含 LM Studio 的真实报错原因
                detail = getattr(e, "_detail", None)   # 重试路径已读过 body，优先复用，避免二次 read 读空
                if detail is None and hasattr(e, "read"):
                    try:
                        detail = e.read().decode("utf-8", "replace")
                    except Exception:
                        detail = ""
                detail = detail or ""
                print(f"  [ERROR] {e}" + (f" | LM Studio: {detail[:800]}" if detail else ""))
                if not headers_sent:                  # headers 未发才能再返回错误，否则只能让连接关闭收尾
                    try:
                        if code and detail:           # 把上游真实状态码+报错原样回传，便于定位（如"模型未加载"）
                            self.send_response(code)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            b = detail.encode()
                            self.send_header("Content-Length", str(len(b)))
                            self.end_headers()
                            self.wfile.write(b)
                        else:
                            self._send({"error": str(e)}, 500)
                    except Exception:
                        pass


def main():
    print("=" * 50)
    print("  Bridge (多线程 + 图片URL)")
    print("=" * 50)

    print(f"\n[CHECK] LM Studio...", end=" ")
    m = call_api(f"{LM_STUDIO_HOST}/v1/models")
    print(f"✅ {len(m['data'])} models" if m else "❌")

    print(f"[CHECK] Draw Things...", end=" ")
    d = call_api(DRAW_THINGS_HOST)
    print(f"✅ {d.get('model','?')}" if d else "❌")

    hp = os.path.join(SCRIPT_DIR, 'chat.html')
    print(f"[CHECK] chat.html...", end=" ")
    print("✅" if os.path.exists(hp) else "❌ 请放到同目录")

    print(f"\n→ 网页: http://<IP>:{BRIDGE_PORT}/")
    print(f"→ API:  http://<IP>:{BRIDGE_PORT}/v1")
    print(f"→ 图片: {IMAGE_DIR}")
    print(f"\n{'=' * 50}\n")

    server = ThreadingHTTPServer(("0.0.0.0", BRIDGE_PORT), BridgeHandler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()

if __name__ == "__main__":
    main()
