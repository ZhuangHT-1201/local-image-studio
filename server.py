from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageOps, UnidentifiedImageError


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


APP_CONFIG = load_config()
COMFY_DIR = Path(
    os.path.expandvars(
        str(APP_CONFIG.get("comfyui_dir") or os.environ.get("COMFYUI_DIR") or r"C:\ComfyUI")
    )
).expanduser()
COMFY_URL = str(
    APP_CONFIG.get("comfyui_url")
    or os.environ.get("COMFYUI_URL")
    or "http://127.0.0.1:8188"
).rstrip("/")
START_SCRIPT = Path(
    os.path.expandvars(
        str(APP_CONFIG.get("start_script") or os.environ.get("COMFYUI_START_SCRIPT") or COMFY_DIR / "Start-ComfyUI.bat")
    )
).expanduser()
FAVICON_PATH = Path(
    os.path.expandvars(
        str(APP_CONFIG.get("icon_path") or os.environ.get("COMFYUI_ICON") or COMFY_DIR.parent / "ComfyUI.ico")
    )
).expanduser()
INPUT_DIR = COMFY_DIR / "input"
OUTPUT_DIR = COMFY_DIR / "output"
MODEL_PROFILES = {
    "sd15": {
        "label": "SD1.5 快速",
        "file": "realisticVisionV60B1_v51VAE.safetensors",
        "cfg": 6.0,
        "max_side": 768,
        "default_width": 512,
        "default_height": 768,
    },
    "sdxl": {
        "label": "SDXL 精品",
        "file": "CyberRealisticXLPlay_V9.0_FP16.safetensors",
        "cfg": 5.0,
        "max_side": 1280,
        "default_width": 832,
        "default_height": 1216,
    },
}
DEFAULT_PROFILE = "sd15"
DEFAULT_NEGATIVE = (
    "child, underage, minor, teen, young-looking, cartoon, anime, illustration, "
    "3d render, painting, cgi, lowres, blurry, worst quality, low quality, "
    "jpeg artifacts, bad anatomy, bad hands, extra fingers, missing fingers, "
    "fused fingers, deformed, disfigured, mutated, extra limbs, watermark, "
    "text, signature, logo"
)

_log_handlers: list[logging.Handler] = [
    RotatingFileHandler(APP_DIR / "server.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
]
if sys.stderr is not None:
    _log_handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("simple-ui")

_comfy_process: subprocess.Popen | None = None
_comfy_lock = threading.Lock()
_started_at: dict[str, float] = {}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _comfy_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 20,
) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    request = Request(COMFY_URL + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        detail = raw.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(detail)
            detail = parsed.get("error") or parsed.get("message") or detail
        except json.JSONDecodeError:
            pass
        raise ApiError(f"ComfyUI 返回错误：{detail[:800]}", status=502) from exc
    except (URLError, TimeoutError) as exc:
        raise ApiError("ComfyUI 尚未就绪，请稍等片刻再试。", status=503) from exc

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("ComfyUI 返回了无法解析的数据。", status=502) from exc


def comfy_is_ready(timeout: float = 1.2) -> bool:
    try:
        _comfy_request("/system_stats", timeout=timeout)
        return True
    except ApiError:
        return False


def ensure_comfy_started() -> bool:
    global _comfy_process
    if comfy_is_ready(timeout=0.7):
        return True

    with _comfy_lock:
        if comfy_is_ready(timeout=0.7):
            return True
        if _comfy_process is not None and _comfy_process.poll() is None:
            return False
        if not START_SCRIPT.exists():
            raise ApiError(f"找不到 ComfyUI 启动脚本：{START_SCRIPT}", status=500)

        log.info("ComfyUI is offline; starting it with %s", START_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _comfy_process = subprocess.Popen(
            ["cmd.exe", "/d", "/c", str(START_SCRIPT)],
            cwd=str(START_SCRIPT.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        return False


def available_models() -> list[str]:
    info = _comfy_request("/object_info/CheckpointLoaderSimple")
    try:
        return list(info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0])
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiError("读取 ComfyUI 模型列表失败。", status=502) from exc


def contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def save_reference_image(data_url: str, width: int, height: int) -> str:
    if not isinstance(data_url, str) or not data_url.startswith("data:image/") or "," not in data_url:
        raise ApiError("参考图格式不正确。", status=400)
    encoded = data_url.split(",", 1)[1]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ApiError("参考图数据损坏，请重新上传。", status=400) from exc
    if len(raw) > 20 * 1024 * 1024:
        raise ApiError("参考图不能超过 20 MB。", status=400)

    try:
        with Image.open(BytesIO(raw)) as opened:
            if opened.width < 32 or opened.height < 32:
                raise ApiError("参考图尺寸过小，请上传至少 32×32 的图片。", status=400)
            if opened.width > 12000 or opened.height > 12000:
                raise ApiError("参考图尺寸过大，请先缩小后再上传。", status=400)
            image = ImageOps.exif_transpose(opened)
            if "A" in image.getbands():
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image = ImageOps.fit(
                image,
                (width, height),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
    except UnidentifiedImageError as exc:
        raise ApiError("无法识别参考图，请使用 PNG、JPEG 或 WebP。", status=400) from exc
    except OSError as exc:
        raise ApiError("参考图读取失败，请换一张图片重试。", status=400) from exc

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"simpleui_ref_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.png"
    image.save(INPUT_DIR / filename, format="PNG", optimize=True)
    return filename


def get_queue() -> dict:
    return _comfy_request("/queue", timeout=5)


def queue_state(prompt_id: str) -> str | None:
    queue = get_queue()
    for item in queue.get("queue_running", []):
        if len(item) > 1 and item[1] == prompt_id:
            return "running"
    for item in queue.get("queue_pending", []):
        if len(item) > 1 and item[1] == prompt_id:
            return "queued"
    return None


def extract_images(entry: dict) -> list[dict]:
    images: list[dict] = []
    for node_output in entry.get("outputs", {}).values():
        for image in node_output.get("images", []):
            if image.get("type") != "output":
                continue
            params = urlencode(
                {
                    "filename": image.get("filename", ""),
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                }
            )
            images.append(
                {
                    "filename": image.get("filename", ""),
                    "subfolder": image.get("subfolder", ""),
                    "url": f"/api/image?{params}",
                    "download_url": f"/api/image?{params}&download=1",
                }
            )
    return images


def extract_error(entry: dict) -> str | None:
    for message in entry.get("status", {}).get("messages", []):
        if not isinstance(message, list) or len(message) < 2:
            continue
        if message[0] != "execution_error":
            continue
        detail = message[1].get("exception_message") or message[1].get("exception_type") or "生成失败"
        if "UnicodeDecodeError" in detail:
            return (
                "DirectML 后端返回了兼容性错误。请确认 ComfyUI 使用了低显存启动参数，"
                "然后关闭本页任务并重新生成。"
            )
        return str(detail).strip()[:800]
    return None


def get_status(prompt_id: str) -> dict:
    try:
        history = _comfy_request(f"/history/{quote(prompt_id, safe='')}", timeout=8)
    except ApiError:
        history = {}

    entry = history.get(prompt_id)
    if entry is not None:
        images = extract_images(entry)
        error = extract_error(entry)
        if error:
            return {"state": "error", "error": error, "images": []}
        if images:
            return {
                "state": "success",
                "images": images,
                "elapsed": round(time.time() - _started_at.get(prompt_id, time.time()), 1),
            }

    try:
        state = queue_state(prompt_id)
    except ApiError:
        state = None

    if state:
        return {
            "state": state,
            "elapsed": round(time.time() - _started_at.get(prompt_id, time.time()), 1),
        }

    if prompt_id in _started_at:
        return {
            "state": "finishing",
            "elapsed": round(time.time() - _started_at.get(prompt_id, time.time()), 1),
        }
    return {"state": "unknown"}


def recent_images(limit: int = 8) -> list[dict]:
    try:
        history = _comfy_request("/history", timeout=12)
    except ApiError:
        return []

    results: list[tuple[float, dict]] = []
    for prompt_id, entry in history.items():
        images = extract_images(entry)
        if not images or not images[0].get("subfolder", "").replace("\\", "/").startswith("SimpleUI"):
            continue
        image_path = OUTPUT_DIR / images[0].get("subfolder", "") / images[0].get("filename", "")
        if not image_path.is_file() or image_path.stat().st_size <= 0:
            continue
        prompt = entry.get("prompt")
        create_time = 0.0
        if isinstance(prompt, list):
            for item in prompt:
                if isinstance(item, dict) and "create_time" in item:
                    create_time = float(item.get("create_time", 0) or 0)
                    break
        if create_time > 10_000_000_000:
            create_time /= 1000
        results.append(
            (
                create_time,
                {
                    "prompt_id": prompt_id,
                    "created_at": datetime.fromtimestamp(create_time).strftime("%m-%d %H:%M")
                    if create_time
                    else "最近",
                    **images[0],
                },
            )
        )
    results.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in results[:limit]]


def generate(body: dict) -> dict:
    if not comfy_is_ready():
        ensure_comfy_started()
        raise ApiError("正在启动 ComfyUI，请等页面显示“已就绪”后再试。", status=503)

    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        raise ApiError("请先填写画面描述。", status=400)
    if len(prompt) > 2000:
        raise ApiError("画面描述过长，请控制在 2000 字以内。", status=400)

    negative = str(body.get("negative_prompt", DEFAULT_NEGATIVE)).strip() or DEFAULT_NEGATIVE
    if len(negative) > 2000:
        raise ApiError("负面提示词过长，请控制在 2000 字以内。", status=400)

    profile_id = str(body.get("model", DEFAULT_PROFILE)).strip().lower()
    profile = MODEL_PROFILES.get(profile_id)
    if profile is None:
        raise ApiError("未知模型选项，请重新打开页面后再试。", status=400)

    try:
        width = int(body.get("width", profile["default_width"]))
        height = int(body.get("height", profile["default_height"]))
        steps = int(body.get("steps", 15))
        cfg = float(body.get("cfg", profile["cfg"]))
    except (TypeError, ValueError) as exc:
        raise ApiError("生成参数格式不正确。", status=400) from exc

    if width < 256 or height < 256 or width > profile["max_side"] or height > profile["max_side"]:
        raise ApiError("画幅超出当前模型的安全范围。", status=400)
    if width % 64 or height % 64:
        raise ApiError("宽高必须是 64 的倍数。", status=400)
    if steps < 5 or steps > 40:
        raise ApiError("步数应在 5 到 40 之间。", status=400)
    if cfg < 1 or cfg > 15:
        raise ApiError("CFG 应在 1 到 15 之间。", status=400)

    models = available_models()
    if profile["file"] not in models:
        raise ApiError(
            f"没有找到模型 {profile['file']}。请检查 checkpoints 目录。",
            status=500,
        )

    raw_seed = body.get("seed")
    if raw_seed in (None, "", -1, "-1"):
        seed = random.SystemRandom().randrange(0, 2**32)
    else:
        try:
            seed = int(raw_seed)
        except (TypeError, ValueError) as exc:
            raise ApiError("种子必须是整数，留空则自动随机。", status=400) from exc
        if seed < 0 or seed > 2**63 - 1:
            raise ApiError("种子超出有效范围。", status=400)

    client_id = f"simple-ui-{uuid.uuid4()}"
    init_data_url = body.get("init_image")
    denoise = 1.0
    mode = "text"
    if init_data_url:
        try:
            denoise = float(body.get("denoise", 0.55))
        except (TypeError, ValueError) as exc:
            raise ApiError("重绘强度格式不正确。", status=400) from exc
        if denoise < 0.25 or denoise > 0.85:
            raise ApiError("重绘强度应在 0.25 到 0.85 之间。", status=400)
        reference_file = save_reference_image(init_data_url, width, height)
        mode = "img2img"

    prefix = (
        f"SimpleUI/{'IMG2IMG' if mode == 'img2img' else profile_id.upper()}/"
        f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    )
    graph = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": profile["file"]},
        },
        "2": {
            "class_type": "ZhPromptEncode",
            "inputs": {
                "clip": ["1", 1],
                "text": prompt,
                "translate": contains_chinese(prompt),
            },
        },
        "3": {
            "class_type": "ZhPromptEncode",
            "inputs": {
                "clip": ["1", 1],
                "text": negative,
                "translate": contains_chinese(negative),
            },
        },
    }

    if mode == "img2img":
        graph["4"] = {
            "class_type": "LoadImage",
            "inputs": {"image": reference_file},
        }
        graph["5"] = {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["4", 0], "vae": ["1", 2]},
        }
        sampler_id = "6"
        decode_id = "7"
        save_id = "8"
        latent_image = ["5", 0]
    else:
        graph["4"] = {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        }
        sampler_id = "5"
        decode_id = "6"
        save_id = "7"
        latent_image = ["4", 0]

    graph[sampler_id] = {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": denoise,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": latent_image,
            },
        }
    graph[decode_id] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": [sampler_id, 0], "vae": ["1", 2]},
        }
    graph[save_id] = {
            "class_type": "SaveImage",
            "inputs": {"images": [decode_id, 0], "filename_prefix": prefix},
        }

    response = _comfy_request(
        "/prompt",
        method="POST",
        payload={"prompt": graph, "client_id": client_id},
        timeout=60,
    )
    if response.get("node_errors"):
        raise ApiError(f"工作流参数有误：{json.dumps(response['node_errors'], ensure_ascii=False)}", status=400)

    prompt_id = response.get("prompt_id")
    if not prompt_id:
        raise ApiError("ComfyUI 没有返回任务编号。", status=502)
    _started_at[prompt_id] = time.time()
    log.info(
        "Queued %s model=%s mode=%s size=%sx%s steps=%s denoise=%s seed=%s",
        prompt_id,
        profile["file"],
        mode,
        width,
        height,
        steps,
        denoise,
        seed,
    )
    return {
        "prompt_id": prompt_id,
        "client_id": client_id,
        "model": profile["file"],
        "profile": profile_id,
        "seed": seed,
        "width": width,
        "height": height,
        "steps": steps,
        "mode": mode,
        "denoise": denoise,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalSimpleImageUI/1.0"

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            log.debug("Client disconnected before the response finished")

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError("请求长度无效。", status=400) from exc
        if length <= 0 or length > 30 * 1024 * 1024:
            raise ApiError("请求内容为空或过大。", status=400)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("请求内容不是有效的 JSON。", status=400) from exc
        if not isinstance(data, dict):
            raise ApiError("请求内容格式不正确。", status=400)
        return data

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                self._send_file(APP_DIR / "index.html", "text/html; charset=utf-8")
                return
            if parsed.path == "/favicon.ico":
                self._send_file(FAVICON_PATH, "image/x-icon")
                return
            if parsed.path == "/api/health":
                ready = ensure_comfy_started()
                models = available_models() if ready else []
                profiles = [
                    {
                        "id": profile_id,
                        "label": profile["label"],
                        "model": profile["file"],
                        "available": profile["file"] in models,
                    }
                    for profile_id, profile in MODEL_PROFILES.items()
                ]
                self._send_json(
                    {
                        "ready": ready,
                        "default_profile": DEFAULT_PROFILE,
                        "default_model": MODEL_PROFILES[DEFAULT_PROFILE]["file"],
                        "model_available": MODEL_PROFILES[DEFAULT_PROFILE]["file"] in models,
                        "profiles": profiles,
                        "models": models,
                        "output_dir": str(OUTPUT_DIR),
                    }
                )
                return
            if parsed.path == "/api/status":
                prompt_id = parse_qs(parsed.query).get("id", [""])[0]
                if not prompt_id:
                    raise ApiError("缺少任务编号。", status=400)
                self._send_json(get_status(prompt_id))
                return
            if parsed.path == "/api/recent":
                self._send_json({"images": recent_images()})
                return
            if parsed.path == "/api/image":
                query = parse_qs(parsed.query)
                filename = query.get("filename", [""])[0]
                subfolder = query.get("subfolder", [""])[0]
                image_type = query.get("type", ["output"])[0]
                download = query.get("download", ["0"])[0] == "1"
                if not filename or Path(filename).name != filename or image_type not in {"output", "temp"}:
                    raise ApiError("图片参数无效。", status=400)
                comfy_query = urlencode(
                    {"filename": filename, "subfolder": subfolder, "type": image_type}
                )
                request = Request(COMFY_URL + "/view?" + comfy_query)
                try:
                    with urlopen(request, timeout=20) as response:
                        body = response.read()
                        content_type = response.headers.get_content_type()
                except (HTTPError, URLError, TimeoutError) as exc:
                    raise ApiError("读取生成图片失败。", status=502) from exc
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=3600")
                if download:
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/open-output":
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(str(OUTPUT_DIR))
                self._send_json({"ok": True})
                return
            self.send_error(404)
        except ApiError as exc:
            self._send_json({"error": exc.message}, status=exc.status)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            log.debug("Client disconnected during GET %s", self.path)
        except Exception:
            log.exception("GET %s failed", self.path)
            self._send_json({"error": "本机服务发生异常，请查看 server.log。"}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path != "/api/generate":
                self.send_error(404)
                return
            self._send_json(generate(self._read_json()))
        except ApiError as exc:
            self._send_json({"error": exc.message}, status=exc.status)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            log.debug("Client disconnected during POST %s", self.path)
        except Exception:
            log.exception("POST %s failed", self.path)
            self._send_json({"error": "生成请求失败，请查看 server.log。"}, status=500)

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = Server(("127.0.0.1", 8190), Handler)
    log.info("Simple UI listening on http://127.0.0.1:8190")
    server.serve_forever()


if __name__ == "__main__":
    main()
