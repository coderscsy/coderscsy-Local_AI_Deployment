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
    m = re.search(r'(?:步数|steps?)\s*(\d+)', text, re.IGNORECASE)
    if m: p["steps"] = int(m.group(1))
    m = re.search(r'(?:宽|width)\s*(\d+)', text, re.IGNORECASE)
    if m: p["width"] = int(m.group(1))
    m = re.search(r'(?:高|height)\s*(\d+)', text, re.IGNORECASE)
    if m: p["height"] = int(m.group(1))
    return p


def extract_description(text):
    s = text
    # 去掉尺寸/步数参数
    s = re.sub(r'(?:步数|steps?)\s*\d+', '', s, flags=re.IGNORECASE)
    s = re.sub(r'(?:宽|width)\s*\d+', '', s, flags=re.IGNORECASE)
    s = re.sub(r'(?:高|height)\s*\d+', '', s, flags=re.IGNORECASE)
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
        t = result["choices"][0]["message"]["content"].strip()
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
                    self._send(f.read(), ct="image/png")
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
        is_stream = body.get("stream", False)
        last_msg = extract_user_message(messages)

        h = self.headers.get("Host", "")
        if h and "100." in h:
            EXTERNAL_HOST = h

        print(f"\n  [IN] stream={is_stream}")
        print(f"  [IN] msg = {last_msg}")

        if is_image_request(last_msg):
            print(f"  [BRIDGE] 🎨 Image!")

            params = parse_params(last_msg)
            description = extract_description(last_msg)
            print(f"  [BRIDGE] desc = {description}")

            if is_stream:
                self._sse_start(model)
                self._sse_text("🎨 正在生成图片，请稍候...\n\n")

                prompt = translate_to_english(description, model)
                print(f"  [BRIDGE] prompt = {prompt}")
                self._sse_text(f"Prompt: {prompt}\n")

                fpath, fname = generate_image(prompt, params)

                if fpath and fname:
                    host = EXTERNAL_HOST or self.headers.get("Host", f"localhost:{BRIDGE_PORT}")
                    url = f"http://{host}/images/{fname}"
                    self._sse_text(
                        f"\n✅ 生成完成！ {params['steps']}步 {params['width']}x{params['height']}\n\n"
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
                    text = (f"🎨 图片已生成！\nPrompt: {prompt}\n"
                            f"{params['steps']}步 {params['width']}x{params['height']}\n\n{url}")
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
            try:
                req = urllib.request.Request(
                    f"{LM_STUDIO_HOST}/v1/chat/completions",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"}, method="POST"
                )
                resp = urllib.request.urlopen(req, timeout=TIMEOUT)
                self.send_response(200)
                ct = resp.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", ct)
                if "event-stream" in ct:
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                else:
                    cl = resp.headers.get("Content-Length")
                    if cl:
                        self.send_header("Content-Length", cl)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                resp.close()
            except Exception as e:
                print(f"  [ERROR] {e}")
                self._send({"error": str(e)}, 500)


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
