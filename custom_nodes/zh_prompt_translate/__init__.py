import logging
import os
import re
import threading

import folder_paths

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
GLOSSARY_PATH = os.path.join(NODE_DIR, "glossary.txt")
MODEL_DIR = os.path.join(folder_paths.models_dir, "translation", "opus-mt-zh-en")

CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
WEIGHT_RE = re.compile(r"^\((.*):\s*([0-9.]+)\)$", re.S)
FULLWIDTH = str.maketrans({"（": "(", "）": ")", "：": ":", "，": ",", "；": ",", "、": ",", "。": ",", "！": ",", "？": ","})

_lock = threading.Lock()
_model = None
_tokenizer = None
_cache = {}
_glossary = []
_glossary_mtime = None


def _load_glossary():
    global _glossary, _glossary_mtime
    try:
        mtime = os.path.getmtime(GLOSSARY_PATH)
    except OSError:
        return []
    if mtime == _glossary_mtime:
        return _glossary
    entries = []
    with open(GLOSSARY_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            zh, en = line.split("=", 1)
            zh, en = zh.strip(), en.strip()
            if zh and en:
                entries.append((zh, en))
    entries.sort(key=lambda e: len(e[0]), reverse=True)
    _glossary, _glossary_mtime = entries, mtime
    return entries


def _get_model():
    global _model, _tokenizer
    if _model is None:
        from transformers import MarianMTModel, MarianTokenizer
        if not os.path.isdir(MODEL_DIR):
            raise RuntimeError(f"Translation model not found: {MODEL_DIR}")
        logging.info(f"[zh_prompt] loading translation model from {MODEL_DIR}")
        _tokenizer = MarianTokenizer.from_pretrained(MODEL_DIR)
        _model = MarianMTModel.from_pretrained(MODEL_DIR).eval()
    return _model, _tokenizer


def _machine_translate(segments):
    import torch
    todo = [s for s in dict.fromkeys(segments) if s not in _cache]
    if todo:
        model, tokenizer = _get_model()
        with torch.inference_mode():
            batch = tokenizer(todo, return_tensors="pt", padding=True, truncation=True, max_length=256)
            out = model.generate(**batch, max_new_tokens=256, num_beams=4)
        for src, dst in zip(todo, tokenizer.batch_decode(out, skip_special_tokens=True)):
            _cache[src] = dst.strip().rstrip(".").strip()
    return [_cache[s] for s in segments]


def _split_top_level(text):
    parts, buf, depth = [], [], 0
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        if (ch == "," or ch == "\n") and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _placeholder(i):
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return "TERM" + name


def _protect_terms(text):
    """Replace glossary terms with placeholders the MT model leaves untouched."""
    terms = []
    for zh, en in _load_glossary():
        if zh in text:
            key = _placeholder(len(terms))
            text = text.replace(zh, f" {key} ")
            terms.append((key, en))
    return text.strip(), terms


def _restore_terms(text, terms):
    missing = []
    for key, en in terms:
        if key in text:
            text = re.sub(rf"\b{key}\b", en, text)
        else:
            missing.append(en)
    return ", ".join([text] + missing) if missing else text


def translate_prompt(text):
    if not CJK_RE.search(text):
        return text
    with _lock:
        text = text.translate(FULLWIDTH)
        glossary = dict(_load_glossary())

        plan = []
        for seg in _split_top_level(text):
            m = WEIGHT_RE.match(seg)
            inner, weight = (m.group(1).strip(), m.group(2)) if m else (seg, None)
            if not CJK_RE.search(inner):
                plan.append((inner, None, [], weight))
            elif inner in glossary:
                plan.append((glossary[inner], None, [], weight))
            else:
                protected, terms = _protect_terms(inner)
                if CJK_RE.search(protected):
                    plan.append((None, protected, terms, weight))
                else:
                    plan.append((_restore_terms(protected, terms), None, [], weight))

        need = [p for _, p, _, _ in plan if p is not None]
        translated = dict(zip(need, _machine_translate(need))) if need else {}

        result = []
        for done, pending, terms, weight in plan:
            en = done if pending is None else _restore_terms(translated[pending], terms)
            en = re.sub(r"\s+", " ", en).strip(" ,")
            if en:
                result.append(f"({en}:{weight})" if weight else en)
        return ", ".join(result)


class ZhPromptEncode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP",),
                "text": ("STRING", {"multiline": True, "dynamicPrompts": True,
                                    "tooltip": "Chinese, English or mixed prompt. Chinese is translated offline."}),
                "translate": ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES = ("CONDITIONING", "STRING")
    RETURN_NAMES = ("conditioning", "english_prompt")
    FUNCTION = "encode"
    CATEGORY = "conditioning/zh_prompt"
    DESCRIPTION = "Translate a Chinese prompt to English locally (glossary + opus-mt-zh-en on CPU), then CLIP-encode it."

    def encode(self, clip, text, translate):
        if clip is None:
            raise RuntimeError("clip input is invalid: None")
        english = translate_prompt(text) if translate else text
        if english != text:
            logging.info(f"[zh_prompt] {text!r} -> {english!r}")
        tokens = clip.tokenize(english)
        return (clip.encode_from_tokens_scheduled(tokens), english)


class ZhTranslateText:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {"multiline": True})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("english",)
    FUNCTION = "run"
    CATEGORY = "conditioning/zh_prompt"

    def run(self, text):
        return (translate_prompt(text),)


NODE_CLASS_MAPPINGS = {
    "ZhPromptEncode": ZhPromptEncode,
    "ZhTranslateText": ZhTranslateText,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ZhPromptEncode": "中文提示词编码 (离线翻译)",
    "ZhTranslateText": "中文翻译为英文 (离线)",
}
