import os
import re

from flask import current_app, has_request_context, request


def get_base_url():
    if has_request_context() and request:
        try:
            forwarded_host = request.headers.get("X-Forwarded-Host")
            if forwarded_host:
                host = forwarded_host.split(",")[0].strip()
                proto = (
                    request.headers.get("X-Forwarded-Proto")
                    or ("https" if request.is_secure else "http")
                ).split(",")[0].strip()
                return f"{proto}://{host}".rstrip("/")

            root = request.url_root
            if root:
                return root.rstrip("/")
        except Exception:
            pass

    if current_app:
        configured = (
            current_app.config.get("BASE_URL")
            or current_app.config.get("APP_BASE_URL")
        )
        if configured:
            return str(configured).strip().rstrip("/")
        server_name = current_app.config.get("SERVER_NAME")
        if server_name:
            scheme = current_app.config.get("PREFERRED_URL_SCHEME", "http")
            return f"{scheme}://{server_name}".rstrip("/")

    env_url = os.environ.get("BASE_URL") or os.environ.get("APP_BASE_URL")
    if env_url:
        return str(env_url).strip().rstrip("/")

    return "http://127.0.0.1:5000"


def build_absolute_url(path=None, base_url=None):
    base = (base_url or get_base_url()).rstrip("/")
    if path is None:
        return base

    path_str = str(path).strip()
    if not path_str:
        return base

    if path_str.startswith("http://") or path_str.startswith("https://") or path_str.startswith("mailto:"):
        return path_str

    if path_str.startswith("//"):
        scheme = "https" if base.startswith("https://") else "http"
        return f"{scheme}:{path_str}"

    clean_path = path_str.lstrip("/")
    return f"{base}/{clean_path}" if clean_path else base


def normalize_plain_text_urls(text, base_url=None):
    if not text or not isinstance(text, str):
        return text

    base = (base_url or get_base_url()).rstrip("/")

    dummy_pattern = re.compile(r"https?://bg(?=[/\s\)\"']|$)(/[^\s\)\"']*)?", re.IGNORECASE)
    text = dummy_pattern.sub(lambda m: f"{base}{m.group(1) or ''}", text)

    def _replace_path(match):
        prefix = match.group(1)
        path = match.group(2)
        if path.startswith("//"):
            return match.group(0)
        return f"{prefix}{build_absolute_url(path, base_url=base)}"

    route_pattern = re.compile(
        r"(^|[\s:;=\"'\(<])(/(?:bg(?:/\d+)?|admin(?:/[\w-]+)?|lifecycle(?:/[\w-]+)?|auth(?:/[\w-]+)?|documents(?:/[\w-]+)?|hub(?:/[\w-]+)?|notifications(?:/[\w-]+)?|profile(?:/[\w-]+)?)(?=[?\s\)\"'>]|$))",
        re.MULTILINE
    )
    text = route_pattern.sub(_replace_path, text)

    return text
