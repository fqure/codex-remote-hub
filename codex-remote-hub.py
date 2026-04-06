#!/usr/bin/env python3
"""
Codex Remote Hub — Access your Codex CLI sessions from any device via Tailscale.
A lightweight web server that manages ttyd + tmux terminal sessions.
"""

from typing import Optional
import subprocess
import os
import sys
import signal
import time
import json
import hashlib
import shutil
import socket
import glob as _glob
import platform as _platform
import re
import mimetypes
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import unquote, parse_qs, urlparse
from urllib.request import urlopen
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as EMAIL_POLICY

VERSION = "1.0.0"

# ─── Platform Detection ─────────────────────────────────────────────────────

PLATFORM = _platform.system().lower()  # 'darwin', 'linux', 'windows'

IS_WSL = False
if PLATFORM == "linux":
    try:
        with open("/proc/version", "r") as f:
            IS_WSL = "microsoft" in f.read().lower()
    except FileNotFoundError:
        pass


def _find_bin(name: str) -> str:
    """Locate a binary on PATH. Returns the name itself as fallback."""
    path = shutil.which(name)
    return path if path else name


def _session_name(name: str) -> str:
    """Build tmux session name, avoiding double 'codex-' prefix."""
    if name.startswith("codex-"):
        return name
    return f"codex-{name}"


# ─── Config ──────────────────────────────────────────────────────────────────

HUB_PORT = int(os.environ.get("CODEX_REMOTE_HUB_PORT", 7690))
BASE_PORT = 7800
MAX_PORT = 7899
def _resolve_bin(env_var: str, name: str) -> str:
    """Get binary path from env var, falling back to PATH lookup if missing or stale."""
    path = os.environ.get(env_var, "")
    if path and os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return _find_bin(name)

TTYD_BIN = _resolve_bin("TTYD_BIN", "ttyd")
TMUX_BIN = _resolve_bin("TMUX_BIN", "tmux")
CODEX_BIN = _resolve_bin("CODEX_BIN", "codex")
FONT_SIZE = int(os.environ.get("CODEX_FONT_SIZE", 11))
DEV_ROOT = os.environ.get("CODEX_DEV_ROOT", os.path.expanduser("~/Projects"))
INSTALL_DIR = os.environ.get("CODEX_REMOTE_HUB_DIR", os.path.expanduser("~/.codex-remote-hub"))

IGNORED_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", ".tox",
                ".mypy_cache", ".pytest_cache", "dist", "build", ".next", ".nuxt"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_template_cache: dict[str, tuple[float, str]] = {}
TTYD_PATCH_MARKER = "codex-remote-hub-mobile-v1"
HUB_AGENTS_START = "<!-- codex-remote-hub:start -->"
HUB_AGENTS_END = "<!-- codex-remote-hub:end -->"

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _ssl_cert_paths() -> tuple[str, str]:
    """Return the configured certificate and key paths."""
    return (
        os.path.join(INSTALL_DIR, "hub.crt"),
        os.path.join(INSTALL_DIR, "hub.key"),
    )


def _codex_remote_shot_command() -> str:
    """Return the screenshot helper command sessions should use."""
    return f"bash {os.path.join(INSTALL_DIR, 'codex-remote-shot')}"


def _safe_path_segment(value: str) -> str:
    """Sanitize a user/session supplied path segment for local storage."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value.strip())
    return cleaned or "default"


def _assets_root_dir() -> str:
    """Return the on-disk root for uploaded/generated session assets."""
    explicit = os.environ.get("CODEX_REMOTE_HUB_ASSETS_DIR", "").strip()
    if explicit:
        return os.path.realpath(os.path.expanduser(explicit))
    return os.path.realpath(os.path.expanduser("~/Pictures/Screenshots"))


def _session_asset_dir(name: str, ensure: bool = False) -> str:
    """Return the per-session asset directory path."""
    asset_dir = os.path.join(_assets_root_dir(), _safe_path_segment(name))
    if ensure:
        os.makedirs(asset_dir, exist_ok=True)
    return asset_dir


def _is_image_filename(filename: str) -> bool:
    """Return True if a filename looks like a supported browser-displayable image."""
    mime_type, _ = mimetypes.guess_type(filename)
    return bool(mime_type and mime_type.startswith("image/"))


def _is_audio_filename(filename: str) -> bool:
    """Return True if a filename looks like a browser-playable audio asset."""
    mime_type, _ = mimetypes.guess_type(filename)
    return bool(mime_type and mime_type.startswith("audio/"))


def _is_session_asset_filename(filename: str) -> bool:
    """Return True for asset types the session browser is allowed to serve."""
    return _is_image_filename(filename) or _is_audio_filename(filename)


def _sanitize_upload_name(filename: str, content_type: str) -> str:
    """Normalize an uploaded filename and preserve/derive an image extension."""
    base = os.path.basename(filename or "").strip()
    stem, ext = os.path.splitext(base)
    safe_stem = _safe_path_segment(stem or "image")
    if not ext:
        guessed_ext = mimetypes.guess_extension(content_type or "") or ".png"
        ext = guessed_ext
    safe_ext = re.sub(r"[^A-Za-z0-9.]", "", ext.lower()) or ".png"
    return safe_stem + safe_ext


def _list_session_assets(name: str) -> list[dict]:
    """Return image assets for a session, newest first."""
    asset_dir = _session_asset_dir(name)
    if not os.path.isdir(asset_dir):
        return []

    assets: list[dict] = []
    try:
        for entry in os.scandir(asset_dir):
            if not entry.is_file():
                continue
            if not _is_image_filename(entry.name):
                continue
            stat = entry.stat()
            assets.append({
                "name": entry.name,
                "path": os.path.realpath(entry.path),
                "url": f"/assets/{_safe_path_segment(name)}/{entry.name}",
                "updated": int(stat.st_mtime * 1000),
                "size": stat.st_size,
            })
    except OSError:
        return []

    assets.sort(key=lambda item: item["updated"], reverse=True)
    return assets


def _parse_uploaded_images(content_type: str, body: bytes) -> list[dict]:
    """Parse multipart image uploads using only the stdlib email parser."""
    if "multipart/form-data" not in (content_type or "").lower():
        return []

    raw = (
        f"Content-Type: {content_type}\r\n"
        f"MIME-Version: 1.0\r\n\r\n"
    ).encode() + body
    message = BytesParser(policy=EMAIL_POLICY).parsebytes(raw)
    if not message.is_multipart():
        return []

    uploads: list[dict] = []
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        uploads.append({
            "filename": filename,
            "content_type": part.get_content_type(),
            "data": payload,
        })
    return uploads


def _store_uploaded_images(name: str, uploads: list[dict]) -> list[dict]:
    """Persist uploaded images for a session and return their metadata."""
    asset_dir = _session_asset_dir(name, ensure=True)
    saved: list[dict] = []
    for upload in uploads:
        content_type = upload.get("content_type", "")
        filename = _sanitize_upload_name(upload.get("filename", ""), content_type)
        if not _is_image_filename(filename):
            continue
        data = upload.get("data", b"")
        if not data or len(data) > 15 * 1024 * 1024:
            continue

        target = os.path.join(asset_dir, filename)
        if os.path.exists(target):
            stem, ext = os.path.splitext(filename)
            target = os.path.join(asset_dir, f"{stem}-{int(time.time() * 1000)}{ext}")

        with open(target, "wb") as f:
            f.write(data)

        stat = os.stat(target)
        saved.append({
            "name": os.path.basename(target),
            "path": os.path.realpath(target),
            "url": f"/assets/{_safe_path_segment(name)}/{os.path.basename(target)}",
            "updated": int(stat.st_mtime * 1000),
            "size": stat.st_size,
        })
    return saved


def _tailscale_dns_name() -> Optional[str]:
    """Return the current machine MagicDNS hostname when Tailscale is available."""
    tailscale = shutil.which("tailscale")
    if not tailscale:
        return None
    try:
        out = subprocess.check_output(
            [tailscale, "status", "--json"],
            text=True, stderr=subprocess.DEVNULL
        )
        dns_name = json.loads(out).get("Self", {}).get("DNSName", "").rstrip(".")
        return dns_name or None
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        return None


def _public_base_url() -> str:
    """Return the best externally reachable base URL for session helpers."""
    explicit = os.environ.get("CODEX_REMOTE_HUB_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit

    scheme = "https" if _has_ssl_certs() else "http"
    host = _tailscale_dns_name() or f"localhost:{HUB_PORT}"
    if ":" not in host:
        host = f"{host}:{HUB_PORT}"
    return f"{scheme}://{host}"


def _hub_agents_block() -> str:
    """Return the managed AGENTS.md block for hub-launched sessions."""
    shot_cmd = _codex_remote_shot_command()
    return (
        f"{HUB_AGENTS_START}\n"
        "## Codex Remote Hub\n"
        f"- When the user asks for a screenshot of this Mac, run `{shot_cmd}` and reply with the returned URL.\n"
        f"- Do not use raw `screencapture` or ad-hoc tmux commands for screenshots when `{shot_cmd}` is available.\n"
        f"- Use `{shot_cmd} window` for the active window and `{shot_cmd} area` for an interactive region when requested.\n"
        "- Return the served URL, not only a local filesystem path.\n"
        f"{HUB_AGENTS_END}\n"
    )


def _dev_root_agents_path(directory: Optional[str]) -> Optional[str]:
    """Return the shared AGENTS.md path when a session lives under DEV_ROOT."""
    try:
        root = os.path.realpath(DEV_ROOT)
        target = os.path.realpath(directory or root)
        if os.path.commonpath([root, target]) != root:
            return None
    except (TypeError, ValueError):
        return None
    return os.path.join(root, "AGENTS.md")


def _ensure_dev_root_agents(directory: Optional[str]) -> None:
    """Upsert the hub-managed screenshot instructions into DEV_ROOT/AGENTS.md."""
    agents_path = _dev_root_agents_path(directory)
    if not agents_path:
        return

    block = _hub_agents_block().rstrip() + "\n"
    existing = ""
    try:
        with open(agents_path, "r", encoding="utf-8") as f:
            existing = f.read()
    except FileNotFoundError:
        existing = ""
    except OSError:
        return

    pattern = re.compile(
        rf"{re.escape(HUB_AGENTS_START)}.*?{re.escape(HUB_AGENTS_END)}\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(block, existing, count=1)
    elif existing.strip():
        updated = existing.rstrip() + "\n\n" + block
    else:
        updated = block

    if updated == existing:
        return

    try:
        os.makedirs(os.path.dirname(agents_path), exist_ok=True)
        with open(agents_path, "w", encoding="utf-8") as f:
            f.write(updated)
    except OSError:
        return


def _session_env(name: str) -> dict[str, str]:
    """Build a child environment for Codex sessions."""
    clean_env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
    clean_env["PATH"] = INSTALL_DIR + os.pathsep + clean_env.get("PATH", "")
    clean_env["CODEX_REMOTE_HUB_ASSETS_DIR"] = _assets_root_dir()
    clean_env["CODEX_REMOTE_HUB_ASSET_DIR"] = _session_asset_dir(name, ensure=True)
    clean_env["CODEX_REMOTE_HUB_SESSION"] = _safe_path_segment(name)
    clean_env["CODEX_REMOTE_HUB_BASE_URL"] = _public_base_url()
    clean_env["CODEX_REMOTE_HUB_SCREENSHOT_CMD"] = _codex_remote_shot_command()
    return clean_env


def _take_session_screenshot(name: str, mode: str = "screen") -> dict:
    """Capture a screenshot for a session and return saved asset metadata."""
    helper_path = os.path.join(INSTALL_DIR, "codex-remote-shot")
    if not os.path.exists(helper_path):
        raise RuntimeError("Screenshot helper not installed")

    normalized_mode = (mode or "screen").strip().lower()
    mode_aliases = {
        "screen": "screen",
        "full": "screen",
        "desktop": "screen",
        "window": "window",
        "area": "area",
        "selection": "area",
    }
    helper_mode = mode_aliases.get(normalized_mode)
    if not helper_mode:
        raise ValueError("invalid screenshot mode")

    proc = subprocess.run(
        ["bash", helper_path, helper_mode],
        capture_output=True,
        text=True,
        env=_session_env(name),
        timeout=180,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "Screenshot capture failed").strip()
        raise RuntimeError(message or "Screenshot capture failed")

    output_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    public_url = ""
    for line in reversed(output_lines):
        if line.startswith("http://") or line.startswith("https://"):
            public_url = line
            break
    if not public_url:
        raise RuntimeError("Screenshot helper did not return a public URL")

    parsed = urlparse(public_url)
    filename = os.path.basename(parsed.path)
    asset_path, _mime_type = _resolve_session_asset_file(name, filename)
    if not asset_path:
        raise RuntimeError("Screenshot file was not saved in the session asset directory")

    stat = os.stat(asset_path)
    return {
        "name": filename,
        "path": asset_path,
        "url": f"/assets/{_safe_path_segment(name)}/{filename}",
        "public_url": public_url,
        "updated": int(stat.st_mtime * 1000),
        "size": stat.st_size,
        "mode": helper_mode,
    }


def _list_macos_windows() -> list[dict]:
    """Return selectable on-screen macOS windows with stable window IDs."""
    if PLATFORM != "darwin":
        return []

    script = (
        'ObjC.import("CoreGraphics"); '
        'ObjC.import("Foundation"); '
        'const opts = $.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements; '
        'const info = $.CGWindowListCopyWindowInfo(opts, $.kCGNullWindowID); '
        'const rows = ObjC.deepUnwrap(ObjC.castRefToObject(info)); '
        'const filtered = rows.map(function(w) { '
        '  const owner = String(w.kCGWindowOwnerName || "").trim(); '
        '  const title = String(w.kCGWindowName || "").trim(); '
        '  return {'
        '    id: Number(w.kCGWindowNumber || 0),'
        '    owner: owner,'
        '    title: title,'
        '    pid: Number(w.kCGWindowOwnerPID || 0),'
        '    layer: Number(w.kCGWindowLayer || 0),'
        '    alpha: Number(w.kCGWindowAlpha || 0),'
        '    onscreen: Boolean(w.kCGWindowIsOnscreen),'
        '    bounds: w.kCGWindowBounds || {}'
        '  };'
        '}).filter(function(w) { '
        '  return w.id > 0 && '
        '    w.layer === 0 && '
        '    w.onscreen && '
        '    w.alpha > 0 && '
        '    w.owner && '
        '    w.owner !== "Window Server" && '
        '    w.owner !== "Control Center"; '
        '}).map(function(w) { '
        '  return {'
        '    id: w.id,'
        '    owner: w.owner,'
        '    title: w.title,'
        '    pid: w.pid,'
        '    bounds: w.bounds'
        '  };'
        '}); '
        'console.log(JSON.stringify(filtered));'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            text=True,
            capture_output=True,
        )
        raw = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0 or not raw:
            return []
        windows = json.loads(raw)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        return []

    deduped: list[dict] = []
    seen: set[tuple[int, str, str]] = set()
    for item in windows:
        try:
            window_id = int(item.get("id", 0))
        except (TypeError, ValueError):
            continue
        owner = str(item.get("owner", "")).strip()
        title = str(item.get("title", "")).strip()
        if not window_id or not owner or not title:
            title = ""
        bounds = item.get("bounds") or {}
        try:
            width = int(bounds.get("Width", 0) or 0)
            height = int(bounds.get("Height", 0) or 0)
        except (TypeError, ValueError):
            width = 0
            height = 0
        if not window_id or not owner:
            continue
        key = (window_id, owner, title)
        if key in seen:
            continue
        seen.add(key)
        if title and title.lower() != owner.lower():
            display_title = title
        elif width > 0 and height > 0:
            display_title = f"Window {window_id} ({width}×{height})"
        else:
            display_title = f"Window {window_id}"
        deduped.append({
            "id": window_id,
            "owner": owner,
            "title": display_title,
            "label": f"{owner} - {display_title}",
        })
    return deduped


def _take_session_window_screenshot(name: str, window_id: int) -> dict:
    """Capture a screenshot for a specific macOS window ID."""
    helper_path = os.path.join(INSTALL_DIR, "codex-remote-shot")
    if not os.path.exists(helper_path):
        raise RuntimeError("Screenshot helper not installed")
    if PLATFORM != "darwin":
        raise RuntimeError("Window screenshots are only supported on macOS")
    if window_id <= 0:
        raise ValueError("invalid window id")

    proc = subprocess.run(
        ["bash", helper_path, "window-id", str(window_id)],
        capture_output=True,
        text=True,
        env=_session_env(name),
        timeout=180,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "Window screenshot failed").strip()
        raise RuntimeError(message or "Window screenshot failed")

    output_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    public_url = ""
    for line in reversed(output_lines):
        if line.startswith("http://") or line.startswith("https://"):
            public_url = line
            break
    if not public_url:
        raise RuntimeError("Screenshot helper did not return a public URL")

    parsed = urlparse(public_url)
    filename = os.path.basename(parsed.path)
    asset_path, _mime_type = _resolve_session_asset_file(name, filename)
    if not asset_path:
        raise RuntimeError("Screenshot file was not saved in the session asset directory")

    stat = os.stat(asset_path)
    return {
        "name": filename,
        "path": asset_path,
        "url": f"/assets/{_safe_path_segment(name)}/{filename}",
        "public_url": public_url,
        "updated": int(stat.st_mtime * 1000),
        "size": stat.st_size,
        "mode": "window",
        "window_id": window_id,
    }


def _resolve_session_asset_file(name: str, filename: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve and validate a session asset path.

    Returns:
        (full_path, content_type)
    """
    safe_name = _safe_path_segment(name)
    requested_name = (filename or "").strip()
    if not requested_name or requested_name != os.path.basename(requested_name):
        return None, None
    if "/" in requested_name or "\\" in requested_name:
        return None, None

    safe_filename = requested_name
    if safe_filename in {"", ".", ".."}:
        return None, None

    if not _is_session_asset_filename(safe_filename):
        return None, None

    asset_dir = _session_asset_dir(safe_name)
    candidate = os.path.realpath(os.path.join(asset_dir, safe_filename))
    asset_root = os.path.realpath(asset_dir)

    if not candidate.startswith(asset_root.rstrip(os.sep) + os.sep):
        if candidate != asset_root:
            return None, None

    if not os.path.isfile(candidate):
        return None, None

    mime_type, _ = mimetypes.guess_type(candidate)
    return candidate, mime_type or "image/png"


def _tts_asset_extension(content_type: str) -> str:
    """Return a filename extension for synthesized speech assets."""
    if content_type == "audio/aiff":
        return ".aiff"
    return ".wav"


def _write_session_tts_asset(name: str, text: str) -> dict:
    """Generate local speech audio and persist it in the session asset directory."""
    audio_bytes, content_type = _synthesize_tts_audio(text)
    safe_name = _safe_path_segment(name)
    asset_dir = _session_asset_dir(safe_name, ensure=True)
    filename = f"tts-reply-{int(time.time() * 1000)}{_tts_asset_extension(content_type)}"
    target_path = os.path.join(asset_dir, filename)

    with open(target_path, "wb") as f:
        f.write(audio_bytes)

    stat = os.stat(target_path)
    return {
        "name": filename,
        "path": target_path,
        "url": f"/assets/{safe_name}/{filename}",
        "content_type": content_type,
        "updated": int(stat.st_mtime * 1000),
        "size": stat.st_size,
    }


def _has_ssl_certs() -> bool:
    """Return True when HTTPS certificates are available for the hub and ttyd."""
    cert_file, key_file = _ssl_cert_paths()
    return os.path.exists(cert_file) and os.path.exists(key_file)


def _synthesize_tts_audio(text: str) -> tuple[bytes, str]:
    """Generate local macOS speech audio for a text payload."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("missing text")
    if len(cleaned) > 12000:
        raise ValueError("text too long")
    if PLATFORM != "darwin":
        raise RuntimeError("Local TTS is only supported on macOS")

    say_bin = shutil.which("say")
    if not say_bin:
        raise RuntimeError("macOS say command not found")
    afconvert_bin = shutil.which("afconvert")

    fd, output_path = tempfile.mkstemp(prefix="codex-remote-hub-", suffix=".aiff")
    wav_fd, wav_path = tempfile.mkstemp(prefix="codex-remote-hub-", suffix=".wav")
    os.close(fd)
    os.close(wav_fd)
    try:
        proc = subprocess.run(
            [say_bin, "-o", output_path, cleaned],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "Speech synthesis failed").strip()
            raise RuntimeError(message or "Speech synthesis failed")
        if afconvert_bin:
            convert_proc = subprocess.run(
                [afconvert_bin, "-f", "WAVE", "-d", "LEI16", output_path, wav_path],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if convert_proc.returncode == 0:
                with open(wav_path, "rb") as f:
                    return f.read(), "audio/wav"

        with open(output_path, "rb") as f:
            return f.read(), "audio/aiff"
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass
        try:
            os.remove(wav_path)
        except OSError:
            pass


def _find_free_port() -> int:
    """Reserve and return an available localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _fetch_default_ttyd_index() -> Optional[str]:
    """Fetch ttyd's built-in index.html by starting a short-lived local instance."""
    port = _find_free_port()
    proc = subprocess.Popen(
        [TTYD_BIN, "-p", str(port), "/bin/sh", "-lc", "sleep 30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if proc.poll() is not None:
                return None
            if _port_in_use_socket(port):
                break
            time.sleep(0.1)
        else:
            return None

        with urlopen(f"http://127.0.0.1:{port}", timeout=2) as response:
            return response.read().decode("utf-8", "replace")
    except Exception:
        return None
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)


def _ensure_ttyd_index() -> None:
    """Generate a patched ttyd index with mobile controls if needed."""
    index_path = os.path.join(INSTALL_DIR, "ttyd-index.html")
    try:
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8", errors="ignore") as f:
                if TTYD_PATCH_MARKER in f.read():
                    return

        patch = _load_template("ttyd-mobile-patch.html")
        default_html = _fetch_default_ttyd_index()
        if not default_html or "</body>" not in default_html:
            return

        patched = default_html.replace("</body>", patch + "\n</body>", 1)
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(patched)
    except OSError:
        pass


def _check_dependencies() -> list[str]:
    """Check that required external tools are installed and return missing ones."""
    missing = []
    for name in ("tmux", "ttyd"):
        if not shutil.which(name):
            missing.append(name)
    return missing


def _dependency_install_hint(name: str) -> str:
    """Return platform-specific install instructions for a missing dependency."""
    hints = {
        "tmux": {
            "darwin": "brew install tmux",
            "linux": "sudo apt install tmux  # or: sudo dnf install tmux / sudo pacman -S tmux",
        },
        "ttyd": {
            "darwin": "brew install ttyd",
            "linux": "sudo snap install ttyd --classic  # or build from source: https://github.com/tsl0922/ttyd",
        },
    }
    platform_key = "darwin" if PLATFORM == "darwin" else "linux"
    return hints.get(name, {}).get(platform_key, f"Install {name} and ensure it is on your PATH")


def _load_template(name: str) -> str:
    """Load an HTML template from templates/, reloading when the file changes."""
    path = os.path.join(SCRIPT_DIR, "templates", name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = -1

    cached = _template_cache.get(name)
    if not cached or cached[0] != mtime:
        with open(path, "r", encoding="utf-8") as f:
            _template_cache[name] = (mtime, f.read())
    return _template_cache[name][1]


def _is_codex_cli_process(command: str) -> bool:
    """Return True if the command string looks like an interactive Codex CLI process."""
    # Must contain 'codex' somewhere
    if "codex" not in command.lower():
        return False
    # Exclude non-CLI processes
    excludes = [
        ".vscode", "codex-remote-hub",
        "ttyd", "--print", "codex_", "electron",
        "node ", "python ", "python3 ",
    ]
    for ex in excludes:
        if ex in command:
            return False
    # Must look like the CLI binary (ends with /codex or is just "codex" with args)
    parts = command.split()
    if not parts:
        return False
    bin_part = parts[0]
    basename = os.path.basename(bin_part)
    return basename == "codex"


def _get_process_cwd(pid: int) -> Optional[str]:
    """Get the current working directory of a process."""
    if PLATFORM == "darwin":
        lsof = shutil.which("lsof") or "/usr/sbin/lsof"
        if not os.path.exists(lsof):
            return None
        try:
            out = subprocess.check_output(
                [lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                text=True, stderr=subprocess.DEVNULL
            )
            for line in out.strip().split("\n"):
                if line.startswith("n") and line != "n":
                    return line[1:]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    else:
        # Linux: /proc/<pid>/cwd symlink
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except (FileNotFoundError, PermissionError, OSError):
            pass
    return None


def _get_repo_root(path: str) -> Optional[str]:
    """Return the enclosing git repo root for a path, if one exists."""
    git = shutil.which("git")
    if not git or not path or not os.path.isdir(path):
        return None
    try:
        out = subprocess.check_output(
            [git, "-C", path, "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if out and os.path.isdir(out):
        return os.path.realpath(out)
    return None


def _extract_workspace_name(command: str) -> Optional[str]:
    """Extract a workspace name from editor extension-host process labels."""
    match = re.search(r"extension-host \([^)]+\) (.+?) \[\d+-\d+\]\s*$", command)
    if not match:
        return None
    workspace = match.group(1).strip()
    return workspace or None


def _resolve_workspace_cwd(workspace_name: str) -> Optional[str]:
    """Resolve a workspace name back to a repo/folder under the dev root."""
    if not workspace_name:
        return None

    search_roots = []
    for base in [
        DEV_ROOT,
        os.path.expanduser("~/Documents/GitHub"),
        os.path.expanduser("~/Projects"),
    ]:
        real_base = os.path.realpath(base)
        if os.path.isdir(real_base) and real_base not in search_roots:
            search_roots.append(real_base)

    matches: set[str] = set()
    for base in search_roots:
        direct_match = os.path.join(base, workspace_name)
        if os.path.isdir(direct_match):
            matches.add(os.path.realpath(direct_match))
            continue

        try:
            for entry in os.scandir(base):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name == workspace_name:
                    matches.add(os.path.realpath(entry.path))
                    continue
                nested_match = os.path.join(entry.path, workspace_name)
                if os.path.isdir(nested_match):
                    matches.add(os.path.realpath(nested_match))
        except OSError:
            continue

    if len(matches) == 1:
        return next(iter(matches))
    return None


def _friendly_process_source(command: str) -> str:
    """Return a short source label when no repo/folder context can be inferred."""
    if "Cursor" in command or ".cursor/" in command:
        return "Cursor"
    if "Codex.app" in command:
        return "Codex"
    return "Codex CLI"


def _find_latest_session_id(cwd: str) -> Optional[str]:
    """Find the most recent Codex session ID for a given project directory."""
    # Codex stores sessions in ~/.codex/sessions/
    codex_dir = os.path.expanduser("~/.codex/sessions")
    if not os.path.isdir(codex_dir):
        return None

    # Find session files sorted by most recent first
    session_files = _glob.glob(os.path.join(codex_dir, "*.jsonl"))
    if not session_files:
        return None

    for filepath in sorted(session_files, key=os.path.getmtime, reverse=True):
        return os.path.splitext(os.path.basename(filepath))[0]

    return None


def port_for_name(name: str) -> int:
    """Generate a deterministic port (7800-7899) from a session name."""
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    return BASE_PORT + (h % (MAX_PORT - BASE_PORT))


def _port_in_use_socket(port: int) -> bool:
    """Check if a port is in use via socket connection attempt."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _get_listening_ports_lsof() -> set[int]:
    """Get listening ports in 7800-7899 range using lsof (macOS/Linux)."""
    lsof = shutil.which("lsof")
    if not lsof:
        return set()
    try:
        out = subprocess.check_output(
            [lsof, f"-iTCP:{BASE_PORT}-{MAX_PORT}", "-sTCP:LISTEN", "-P", "-n"],
            text=True, stderr=subprocess.DEVNULL
        )
        ports: set[int] = set()
        for line in out.strip().split("\n"):
            if "LISTEN" in line:
                for part in line.split():
                    if ":" in part and part.split(":")[-1].isdigit():
                        ports.add(int(part.split(":")[-1]))
        return ports
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()


def _get_listening_ports_ss() -> set[int]:
    """Get listening ports in 7800-7899 range using ss (Linux)."""
    ss = shutil.which("ss")
    if not ss:
        return set()
    try:
        out = subprocess.check_output(
            [ss, "-tlnH"], text=True, stderr=subprocess.DEVNULL
        )
        ports: set[int] = set()
        for line in out.strip().split("\n"):
            parts = line.split()
            for part in parts:
                if ":" in part:
                    port_str = part.rsplit(":", 1)[-1]
                    if port_str.isdigit():
                        port = int(port_str)
                        if BASE_PORT <= port <= MAX_PORT:
                            ports.add(port)
        return ports
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()


def get_ttyd_ports() -> set[int]:
    """Return the set of ports where ttyd is currently listening."""
    ports = _get_listening_ports_lsof()
    if not ports and PLATFORM == "linux":
        ports = _get_listening_ports_ss()
    return ports


def port_in_use(port: int) -> bool:
    """Check if a TCP port is currently in use."""
    lsof = shutil.which("lsof")
    if lsof:
        r = subprocess.run([lsof, "-i", f":{port}"], capture_output=True)
        return r.returncode == 0

    ss = shutil.which("ss")
    if ss:
        r = subprocess.run(
            [ss, "-tlnH", f"sport = :{port}"],
            capture_output=True, text=True
        )
        return bool(r.stdout.strip())

    return _port_in_use_socket(port)


def _cleanup_orphan_ttyd() -> None:
    """Kill ttyd processes whose tmux sessions no longer exist."""
    try:
        ps_out = subprocess.check_output(
            ["ps", "-ww", "-eo", "pid,command"],
            text=True, stderr=subprocess.DEVNULL
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    for line in ps_out.strip().split("\n"):
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, cmd = parts
        if not pid_str.strip().isdigit():
            continue
        if "ttyd" not in cmd or "attach-session" not in cmd:
            continue
        # Extract session name from "... attach-session -t <session>"
        cmd_parts = cmd.split()
        session_name = None
        for i, p in enumerate(cmd_parts):
            if p == "attach-session" and i + 2 < len(cmd_parts) and cmd_parts[i + 1] == "-t":
                session_name = cmd_parts[i + 2]
                break
        if not session_name:
            continue
        # Check if tmux session exists
        r = subprocess.run(
            [TMUX_BIN, "has-session", "-t", session_name],
            capture_output=True
        )
        if r.returncode != 0:
            try:
                os.kill(int(pid_str.strip()), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def get_sessions() -> list[dict]:
    """List active Codex tmux sessions with their status."""
    _cleanup_orphan_ttyd()
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            tmux_future = ex.submit(
                subprocess.check_output,
                [TMUX_BIN, "list-sessions", "-F",
                 "#{session_name}|#{session_activity}|#{session_windows}|#{session_attached}"],
                text=True, stderr=subprocess.DEVNULL
            )
            ports_future = ex.submit(get_ttyd_ports)
            out = tmux_future.result(timeout=3)
            ttyd_ports = ports_future.result(timeout=3)
        sessions: list[dict] = []
        for line in out.strip().split("\n"):
            if not line.startswith("codex-"):
                continue
            parts = line.split("|")
            name = parts[0].removeprefix("codex-")
            try:
                last_activity = datetime.fromtimestamp(int(parts[1]))
                time_str = last_activity.strftime("%H:%M")
            except (ValueError, IndexError):
                time_str = "?"
            attached = parts[3] if len(parts) > 3 else "0"
            port = port_for_name(name)
            sessions.append({
                "name": name,
                "port": port,
                "time": time_str,
                "attached": attached != "0",
                "has_ttyd": port in ttyd_ports,
            })
        return sessions
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def discover_capturable_sessions() -> list:
    """Find Codex CLI processes running outside the hub's tmux sessions."""
    # Step 1: Get PIDs of all tmux pane processes (these are managed by us)
    tmux_pids: set = set()
    try:
        out = subprocess.check_output(
            [TMUX_BIN, "list-panes", "-a", "-F", "#{pane_pid}"],
            text=True, stderr=subprocess.DEVNULL
        )
        for line in out.strip().split("\n"):
            if line.strip().isdigit():
                tmux_pids.add(int(line.strip()))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Also collect all descendant PIDs of tmux panes
    tmux_tree_pids: set = set(tmux_pids)
    if tmux_pids:
        try:
            ps_out = subprocess.check_output(
                ["ps", "-eo", "pid,ppid"], text=True, stderr=subprocess.DEVNULL
            )
            # Build parent->children map
            children_map: dict = {}
            for line in ps_out.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    child_pid = int(parts[0])
                    parent_pid = int(parts[1])
                    children_map.setdefault(parent_pid, []).append(child_pid)
            # BFS to find all descendants
            queue = list(tmux_pids)
            while queue:
                p = queue.pop(0)
                for child in children_map.get(p, []):
                    if child not in tmux_tree_pids:
                        tmux_tree_pids.add(child)
                        queue.append(child)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    # Step 2: List all processes
    try:
        ps_out = subprocess.check_output(
            ["ps", "-eo", "pid,ppid,tty,command"],
            text=True, stderr=subprocess.DEVNULL
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    process_rows: dict[int, dict] = {}
    for line in ps_out.strip().split("\n")[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        process_rows[pid] = {
            "pid": pid,
            "ppid": ppid,
            "tty": parts[2],
            "command": parts[3],
        }

    capturable = []
    for proc in process_rows.values():
        pid = proc["pid"]
        ppid = proc["ppid"]
        tty = proc["tty"]
        command = proc["command"]

        # Skip processes inside tmux
        if pid in tmux_tree_pids:
            continue

        # Check if this is a Codex CLI process
        if not _is_codex_cli_process(command):
            continue

        # Get CWD
        cwd = _get_process_cwd(pid)
        if not cwd:
            continue

        repo_root = _get_repo_root(cwd) if cwd != "/" else None
        if repo_root:
            cwd = repo_root

        project_name = os.path.basename(cwd.rstrip(os.sep)) if cwd not in {"", "/"} else ""
        parent_command = process_rows.get(ppid, {}).get("command", "")

        if not project_name:
            workspace_name = _extract_workspace_name(parent_command)
            if workspace_name:
                inferred_cwd = _resolve_workspace_cwd(workspace_name)
                if inferred_cwd:
                    repo_root = _get_repo_root(inferred_cwd) or inferred_cwd
                    cwd = repo_root
                project_name = workspace_name

        if not project_name:
            project_name = _friendly_process_source(command)

        session_id = _find_latest_session_id(cwd)

        capturable.append({
            "pid": pid,
            "tty": tty,
            "cwd": cwd,
            "project_name": project_name,
            "session_id": session_id,
        })

    return capturable


def get_folders(rel_path: str = "") -> dict:
    """List subdirectories under DEV_ROOT for the folder picker."""
    base = os.path.realpath(DEV_ROOT)
    if not os.path.isdir(base):
        base = os.path.expanduser("~")

    target = os.path.realpath(os.path.join(base, rel_path)) if rel_path else base

    if not target.startswith(base):
        target = base
    if not os.path.isdir(target):
        target = base

    folders: list[str] = []

    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith(".") and entry.name not in IGNORED_DIRS:
                folders.append(entry.name)
    except (PermissionError, FileNotFoundError, OSError):
        pass

    display_path = os.path.relpath(target, base)
    if display_path == ".":
        display_path = ""

    return {
        "folders": folders,
        "current": display_path,
        "absolute": target,
        "can_go_up": target != base,
        "root_name": os.path.basename(base),
    }


def _kill_ttyd_on_port(port: int) -> None:
    """Kill any ttyd process listening on the given port."""
    try:
        out = subprocess.check_output(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            text=True, stderr=subprocess.DEVNULL
        )
        for line in out.strip().split("\n"):
            pid = line.strip()
            if pid.isdigit():
                os.kill(int(pid), signal.SIGTERM)
        time.sleep(0.2)
    except (subprocess.CalledProcessError, FileNotFoundError, ProcessLookupError):
        pass


def _ttyd_session_on_port(port: int) -> Optional[str]:
    """Return the tmux session name that a ttyd on this port is attached to, or None."""
    try:
        out = subprocess.check_output(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            text=True, stderr=subprocess.DEVNULL
        )
        for line in out.strip().split("\n"):
            pid = line.strip()
            if not pid.isdigit():
                continue
            cmd_out = subprocess.check_output(
                ["ps", "-ww", "-p", pid, "-o", "command="],
                text=True, stderr=subprocess.DEVNULL
            )
            # Extract session name from "... attach-session -t <session>"
            parts = cmd_out.strip().split()
            for i, part in enumerate(parts):
                if part == "attach-session" and i + 2 < len(parts) and parts[i + 1] == "-t":
                    return parts[i + 2]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


def _start_ttyd(session: str, port: int) -> None:
    """Start a ttyd process attached to a tmux session if not already running."""
    if port_in_use(port):
        existing = _ttyd_session_on_port(port)
        if existing == session:
            return
        # Port occupied by ttyd for a different/dead session; reclaim it
        _kill_ttyd_on_port(port)

    _ensure_ttyd_index()

    ttyd_cmd = [
        TTYD_BIN, "-W", "-p", str(port),
        "--ping-interval", "5",
        "-t", f"fontSize={FONT_SIZE}",
        "-t", 'theme={"background":"#0f0f1a","foreground":"#e8e8f0","cursor":"#10a37f"}',
        "-t", "titleFixed=Codex Remote Hub",
    ]
    # Custom index file for virtual keyboard overlay
    custom_index = os.path.join(INSTALL_DIR, "ttyd-index.html")
    if os.path.exists(custom_index):
        ttyd_cmd += ["-I", custom_index]

    # HTTPS: use certs if available
    cert_file, key_file = _ssl_cert_paths()
    if _has_ssl_certs():
        ttyd_cmd += ["-S", "-C", cert_file, "-K", key_file]

    ttyd_cmd += ["tmux", "attach-session", "-t", session]
    subprocess.Popen(
        ttyd_cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(0.3)


def start_session(name: str, directory: Optional[str] = None, skip_permissions: bool = False) -> int:
    """Start a tmux + ttyd session. Returns the assigned port."""
    port = port_for_name(name)
    session = _session_name(name)

    r = subprocess.run([TMUX_BIN, "has-session", "-t", session],
                       capture_output=True)
    if r.returncode != 0:
        _ensure_dev_root_agents(directory)
        cmd = [TMUX_BIN, "new-session", "-d", "-s", session]
        if directory and os.path.isdir(directory):
            cmd += ["-c", directory]
        cmd.append(CODEX_BIN)
        if skip_permissions:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        # Strip CODEX_HOME to prevent "cannot launch inside another session" error
        clean_env = _session_env(name)
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=clean_env
        )
        time.sleep(0.5)
        subprocess.run([TMUX_BIN, "set-option", "-t", session, "mouse", "on"],
                       capture_output=True)

    _start_ttyd(session, port)
    return port


def capture_session(pid: int, session_id: Optional[str], cwd: str,
                    name: str, skip_permissions: bool = False) -> tuple:
    """Capture a running Codex CLI session into a tmux + ttyd session.

    Uses `codex fork <session_id>` to branch the conversation into a new tmux session.
    Returns the assigned port.
    """
    # Ensure unique session name
    base_name = name
    suffix = 1
    while True:
        session = _session_name(name)
        r = subprocess.run([TMUX_BIN, "has-session", "-t", session],
                           capture_output=True)
        if r.returncode != 0:
            break
        suffix += 1
        name = f"{base_name}-{suffix}"

    session = _session_name(name)
    port = port_for_name(name)
    _ensure_dev_root_agents(cwd)

    # Build the codex command with fork or resume --last
    cmd = [TMUX_BIN, "new-session", "-d", "-s", session, "-x", "200", "-y", "50"]
    if cwd and os.path.isdir(cwd):
        cmd += ["-c", cwd]

    if session_id:
        cmd += [CODEX_BIN, "fork", session_id]
    else:
        cmd += [CODEX_BIN, "resume", "--last"]
    if skip_permissions:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")

    # Strip CODEX_HOME to prevent "cannot launch inside another session" error
    clean_env = _session_env(name)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env=clean_env)
    time.sleep(1.0)

    # Verify the tmux session survived (codex might have failed and exited)
    r = subprocess.run([TMUX_BIN, "has-session", "-t", session],
                       capture_output=True)
    if r.returncode != 0:
        # fork/resume failed — try resume --last as fallback
        if session_id:
            cmd_fallback = [TMUX_BIN, "new-session", "-d", "-s", session, "-x", "200", "-y", "50"]
            if cwd and os.path.isdir(cwd):
                cmd_fallback += ["-c", cwd]
            cmd_fallback += [CODEX_BIN, "resume", "--last"]
            if skip_permissions:
                cmd_fallback.append("--dangerously-bypass-approvals-and-sandbox")
            subprocess.Popen(cmd_fallback, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=clean_env)
            time.sleep(1.0)

    # Final check — if still no session, start fresh codex so ttyd has something to connect to
    r = subprocess.run([TMUX_BIN, "has-session", "-t", session],
                       capture_output=True)
    if r.returncode != 0:
        fallback_cmd = [TMUX_BIN, "new-session", "-d", "-s", session, "-x", "200", "-y", "50"]
        if cwd and os.path.isdir(cwd):
            fallback_cmd += ["-c", cwd]
        fallback_cmd.append(CODEX_BIN)
        if skip_permissions:
            fallback_cmd.append("--dangerously-bypass-approvals-and-sandbox")
        subprocess.Popen(fallback_cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=clean_env)
        time.sleep(0.5)

    subprocess.run([TMUX_BIN, "set-option", "-t", session, "mouse", "on"],
                   capture_output=True)

    _start_ttyd(session, port)
    return port, name


def stop_session(name: str) -> None:
    """Stop ttyd and kill the tmux session."""
    port = port_for_name(name)
    session = _session_name(name)

    pkill = shutil.which("pkill")
    if pkill:
        subprocess.run([pkill, "-f", f"ttyd.*-p {port}"],
                       capture_output=True)
    else:
        # Fallback: find and kill ttyd process via port
        try:
            lsof = shutil.which("lsof")
            if lsof:
                out = subprocess.check_output(
                    [lsof, "-ti", f":{port}"], text=True, stderr=subprocess.DEVNULL
                ).strip()
                for pid_str in out.split("\n"):
                    if pid_str.isdigit():
                        os.kill(int(pid_str), signal.SIGTERM)
        except (subprocess.CalledProcessError, ValueError):
            pass

    subprocess.run([TMUX_BIN, "kill-session", "-t", session],
                   capture_output=True)


# ─── HTML Rendering ─────────────────────────────────────────────────────────

def render_hub(host: str) -> str:
    """Render the dashboard with active sessions."""
    sessions = get_sessions()

    session_cards = ""
    for s in sessions:
        status_class = "active" if s["has_ttyd"] else "idle"
        attached_badge = '<span class="badge active">connected</span>' if s["attached"] else ""
        session_cards += f"""
        <div class="card">
          <a href="/start/{s['name']}" class="card-link">
            <div class="card-left">
              <span class="status-dot {status_class}"></span>
              <div>
                <div class="card-name">{s['name']}</div>
                <div class="card-meta">port {s['port']} &middot; {s['time']}</div>
              </div>
            </div>
            <div class="card-right">
              {attached_badge}
              <span class="arrow">&rsaquo;</span>
            </div>
          </a>
          <button class="stop-btn" onclick="event.preventDefault();if(confirm('Stop session {s['name']}?'))location='/stop/{s['name']}'">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 1l12 12M13 1L1 13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
          </button>
        </div>"""

    if not sessions:
        session_cards = """
        <div class="empty">
          <svg class="empty-icon" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line>
          </svg>
          <p>No active sessions</p>
          <p class="empty-sub">Create one below to get started</p>
        </div>"""

    count = len(sessions)
    count_text = f"{count} active session" if count == 1 else f"{count} active sessions"

    html = _load_template("hub.html")
    return (html
            .replace("{{COUNT_TEXT}}", count_text)
            .replace("{{SESSION_CARDS}}", session_cards)
            .replace("{{VERSION}}", VERSION))


def render_terminal(name: str, port: int, host: str) -> str:
    """Render the terminal wrapper page."""
    scheme = "https" if _has_ssl_certs() else "http"
    terminal_url = f"{scheme}://{host}:{port}"
    html = _load_template("terminal.html")
    return html.replace("{{SESSION_NAME}}", name).replace("{{TERMINAL_URL}}", terminal_url)


def render_mobile_terminal(name: str, port: int, host: str) -> str:
    """Render the mobile-first terminal shell for iPhone/iPad."""
    scheme = "https" if _has_ssl_certs() else "http"
    terminal_url = f"{scheme}://{host}:{port}"
    html = _load_template("mobile.html")
    return (html
            .replace("{{SESSION_NAME}}", name)
            .replace("{{TERMINAL_URL}}", terminal_url))


def get_pane_snapshot(name: str, lines: int = 160) -> dict:
    """Return a plain-text snapshot of the tmux pane for mobile rendering."""
    session = _session_name(name)
    lines = max(40, min(lines, 400))

    has_session = subprocess.run(
        [TMUX_BIN, "has-session", "-t", session],
        capture_output=True
    )
    if has_session.returncode != 0:
        return {"ok": False, "missing": True}

    capture = subprocess.run(
        [TMUX_BIN, "capture-pane", "-p", "-J", "-t", session, "-S", "-", "-E", "-"],
        capture_output=True, text=True
    )
    path_proc = subprocess.run(
        [TMUX_BIN, "display-message", "-p", "-t", session, "#{pane_current_path}"],
        capture_output=True, text=True
    )
    title_proc = subprocess.run(
        [TMUX_BIN, "display-message", "-p", "-t", session, "#{session_name}"],
        capture_output=True, text=True
    )
    mode_proc = subprocess.run(
        [TMUX_BIN, "display-message", "-p", "-t", session, "#{pane_in_mode}"],
        capture_output=True, text=True
    )
    cursor_proc = subprocess.run(
        [TMUX_BIN, "display-message", "-p", "-t", session, "#{cursor_y}"],
        capture_output=True, text=True
    )

    text = capture.stdout if capture.returncode == 0 else ""
    try:
        cursor_y = int(cursor_proc.stdout.strip())
    except (TypeError, ValueError):
        cursor_y = None
    return {
        "ok": True,
        "text": text,
        "cwd": path_proc.stdout.strip() if path_proc.returncode == 0 else "",
        "title": title_proc.stdout.strip() if title_proc.returncode == 0 else name,
        "copy_mode": mode_proc.stdout.strip() == "1",
        "cursor_y": cursor_y,
        "updated": int(time.time() * 1000),
    }


# ─── HTTP Handler ────────────────────────────────────────────────────────────

class HubHandler(BaseHTTPRequestHandler):
    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        # Start session
        if path.startswith("/start/"):
            name = path.split("/start/")[1].strip("/")
            if not name:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            directory = qs.get("dir", [None])[0]
            skip_permissions = qs.get("skip_permissions", ["0"])[0] == "1"
            start_session(name, directory, skip_permissions)
            self.send_response(302)
            self.send_header("Location", f"/terminal/{name}")
            self.end_headers()
            return

        # Terminal wrapper
        if path.startswith("/terminal/"):
            name = path.split("/terminal/")[1].strip("/")
            if not name:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            port = port_for_name(name)
            host = self.headers.get("Host", "localhost").split(":")[0]
            html = render_terminal(name, port, host)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(html.encode())
            return

        # Mobile-first terminal shell
        if path.startswith("/mobile/"):
            name = path.split("/mobile/")[1].strip("/")
            if not name:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            port = port_for_name(name)
            host = self.headers.get("Host", "localhost").split(":")[0]
            html = render_mobile_terminal(name, port, host)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(html.encode())
            return

        # Stop session
        if path.startswith("/stop/"):
            name = path.split("/stop/")[1].strip("/")
            stop_session(name)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return

        # API: list sessions (JSON)
        if path == "/api/sessions":
            sessions = get_sessions()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(sessions).encode())
            return

        # API: check if ttyd is ready
        if path.startswith("/api/ttyd-ready/"):
            name = path.split("/api/ttyd-ready/")[1].strip("/")
            port = port_for_name(name)
            ready = port_in_use(port)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            self.wfile.write(json.dumps({"ready": ready, "port": port}).encode())
            return

        # API: pane snapshot for mobile view
        if path.startswith("/api/pane/"):
            name = path.split("/api/pane/")[1].strip("/")
            try:
                lines = int(qs.get("lines", ["160"])[0])
            except (ValueError, IndexError):
                lines = 160
            data = get_pane_snapshot(name, lines)
            self.send_response(200 if data.get("ok") else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
            return

        if path.startswith("/api/tts-file/"):
            name = path.split("/api/tts-file/")[1].strip("/")
            if not name:
                self._send_json({"error": "invalid session name"}, 400)
                return
            text = qs.get("text", [""])[0]
            try:
                asset = _write_session_tts_asset(name, text)
            except ValueError as err:
                self._send_json({"error": str(err)}, 400)
                return
            except subprocess.TimeoutExpired:
                self._send_json({"error": "Speech synthesis timed out"}, 504)
                return
            except RuntimeError as err:
                self._send_json({"error": str(err)}, 500)
                return
            except OSError:
                self._send_json({"error": "Failed to save speech audio"}, 500)
                return

            self._send_json(asset)
            return

        if path.startswith("/api/tts/"):
            name = path.split("/api/tts/")[1].strip("/")
            if not name:
                self._send_json({"error": "invalid session name"}, 400)
                return
            text = qs.get("text", [""])[0]
            try:
                audio_bytes, content_type = _synthesize_tts_audio(text)
            except ValueError as err:
                self._send_json({"error": str(err)}, 400)
                return
            except subprocess.TimeoutExpired:
                self._send_json({"error": "Speech synthesis timed out"}, 504)
                return
            except RuntimeError as err:
                self._send_json({"error": str(err)}, 500)
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.end_headers()
            self.wfile.write(audio_bytes)
            return

        # API: list capturable sessions (JSON)
        if path == "/api/capturable":
            sessions = discover_capturable_sessions()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            self.wfile.write(json.dumps(sessions).encode())
            return

        # API: list selectable macOS windows for screenshot capture
        if path == "/api/windows":
            windows = _list_macos_windows()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "windows": windows}).encode())
            return

        # Capture a running Codex CLI session
        if path == "/capture":
            try:
                pid = int(qs.get("pid", [0])[0])
            except (ValueError, IndexError):
                pid = 0
            cwd = qs.get("cwd", [""])[0]
            session_id = qs.get("session_id", [None])[0]
            name = qs.get("name", [""])[0]
            skip_permissions = qs.get("skip_permissions", ["0"])[0] == "1"

            if not pid or not name:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return

            # Verify the process still exists
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                self.send_response(302)
                self.send_header("Location", "/?error=process_gone")
                self.end_headers()
                return

            port, final_name = capture_session(pid, session_id, cwd, name, skip_permissions)
            self.send_response(302)
            self.send_header("Location", f"/terminal/{final_name}")
            self.end_headers()
            return

        # Download SSL certificate
        if path == "/cert":
            cert_path = os.path.join(INSTALL_DIR, "hub.crt")
            if os.path.exists(cert_path):
                with open(cert_path, "rb") as f:
                    cert_data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-x509-ca-cert")
                self.send_header("Content-Disposition", "attachment; filename=codex-remote-hub.crt")
                self.end_headers()
                self.wfile.write(cert_data)
            else:
                self.send_response(404)
                self.end_headers()
            return

        # API: list folders
        if path == "/api/folders":
            rel_path = qs.get("path", [""])[0]
            data = get_folders(rel_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
            return

        # API: list image assets for session
        if path.startswith("/api/assets/"):
            name = path.split("/api/assets/")[1].strip("/")
            if not name:
                self.send_response(400)
                self.end_headers()
                return
            assets = _list_session_assets(name)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            self.wfile.write(json.dumps({"assets": assets}).encode())
            return

        # Asset download (and browser preview)
        if path.startswith("/assets/"):
            raw = path[len("/assets/"):]
            if "/" not in raw:
                self.send_response(404)
                self.end_headers()
                return
            name, filename = raw.split("/", 1)
            asset_path, mime_type = _resolve_session_asset_file(name, filename)
            if not asset_path:
                self.send_response(404)
                self.end_headers()
                return
            try:
                with open(asset_path, "rb") as f:
                    data = f.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            if qs.get("download", ["0"])[0] == "1":
                filename = os.path.basename(asset_path)
                safe_name = filename.replace('"', "")
                self.send_header(
                    "Content-Disposition", f'attachment; filename=\"{safe_name}\"'
                )
            self.end_headers()
            self.wfile.write(data)
            return

        # Icon
        if path == "/icon.png":
            icon_path = os.path.join(INSTALL_DIR, "icon_cxhub.png")
            if not os.path.exists(icon_path):
                icon_path = os.path.join(SCRIPT_DIR, "icon_cxhub.png")
            if os.path.exists(icon_path):
                with open(icon_path, "rb") as f:
                    icon_data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(icon_data)
            else:
                self.send_response(404)
                self.end_headers()
            return

        # Hub dashboard
        host = self.headers.get("Host", f"localhost:{HUB_PORT}")
        html = render_hub(host)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html.encode())

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path.startswith("/api/tts-file/"):
            name = path.split("/api/tts-file/")[1].strip("/")
            if not name:
                self._send_json({"error": "invalid session name"}, 400)
                return

            content_length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(content_length) or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "invalid json"}, 400)
                return

            try:
                asset = _write_session_tts_asset(name, payload.get("text", ""))
            except ValueError as err:
                self._send_json({"error": str(err)}, 400)
                return
            except subprocess.TimeoutExpired:
                self._send_json({"error": "Speech synthesis timed out"}, 504)
                return
            except RuntimeError as err:
                self._send_json({"error": str(err)}, 500)
                return
            except OSError:
                self._send_json({"error": "Failed to save speech audio"}, 500)
                return

            self._send_json(asset)
            return

        if path.startswith("/api/tts/"):
            name = path.split("/api/tts/")[1].strip("/")
            if not name:
                self._send_json({"error": "invalid session name"}, 400)
                return

            content_length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(content_length) or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "invalid json"}, 400)
                return

            try:
                audio_bytes, content_type = _synthesize_tts_audio(payload.get("text", ""))
            except ValueError as err:
                self._send_json({"error": str(err)}, 400)
                return
            except subprocess.TimeoutExpired:
                self._send_json({"error": "Speech synthesis timed out"}, 504)
                return
            except RuntimeError as err:
                self._send_json({"error": str(err)}, 500)
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Content-Length", str(len(audio_bytes)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(audio_bytes)
            return

        # API: take a native screenshot for a session
        if path.startswith("/api/screenshot/"):
            name = path.split("/api/screenshot/")[1].strip("/")
            if not name:
                self._send_json({"error": "invalid session name"}, 400)
                return

            content_length = int(self.headers.get("Content-Length", 0))
            payload = {}
            if content_length:
                try:
                    payload = json.loads(self.rfile.read(content_length))
                except json.JSONDecodeError:
                    self._send_json({"error": "invalid json"}, 400)
                    return

            try:
                requested_mode = payload.get("mode", "screen")
                if requested_mode == "window":
                    screenshot = _take_session_window_screenshot(
                        name,
                        int(payload.get("window_id", 0)),
                    )
                else:
                    screenshot = _take_session_screenshot(name, requested_mode)
            except ValueError:
                self._send_json({"error": "invalid screenshot request"}, 400)
                return
            except subprocess.TimeoutExpired:
                self._send_json({"error": "Screenshot capture timed out"}, 504)
                return
            except RuntimeError as err:
                self._send_json({"error": str(err)}, 500)
                return

            self._send_json({"ok": True, "screenshot": screenshot})
            return

        # API: upload image attachments for a session
        if path.startswith("/api/upload-image/"):
            name = path.split("/api/upload-image/")[1].strip("/")
            if not name:
                self._send_json({"error": "invalid session name"}, 400)
                return

            content_type = self.headers.get("Content-Type", "")
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            uploads = _parse_uploaded_images(content_type, body)
            if not uploads:
                self._send_json({"error": "no images uploaded"}, 400)
                return
            saved = _store_uploaded_images(name, uploads)
            self._send_json({
                "ok": True,
                "saved": saved,
                "count": len(saved),
            })
            return

        # API: send special key via tmux
        if path.startswith("/api/send-keys/"):
            name = path.split("/api/send-keys/")[1].strip("/")
            session = _session_name(name)
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            key = data.get("key", "")

            allowed_keys = {
                "Escape", "Tab", "BTab", "Enter", "Space",
                "Up", "Down", "Left", "Right",
                "C-c", "C-v", "C-z", "C-d", "C-l", "C-a", "C-e",
                "C-r", "C-w", "C-u", "C-k", "C-b", "C-f", "C-n", "C-p",
            }

            if key not in allowed_keys:
                self._send_json({"error": "key not allowed"}, 400)
                return

            subprocess.run(
                [TMUX_BIN, "send-keys", "-t", session, key],
                capture_output=True
            )
            self._send_json({"ok": True})
            return

        # API: send text (paste) via tmux
        if path.startswith("/api/send-text/"):
            name = path.split("/api/send-text/")[1].strip("/")
            session = _session_name(name)
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            text = data.get("text", "")

            if not text or len(text) > 10000:
                self._send_json({"error": "invalid text"}, 400)
                return

            proc = subprocess.run(
                [TMUX_BIN, "load-buffer", "-"],
                input=text, capture_output=True, text=True
            )
            if proc.returncode == 0:
                subprocess.run(
                    [TMUX_BIN, "paste-buffer", "-t", session],
                    capture_output=True
                )

            self._send_json({"ok": True})
            return

        # API: scroll via tmux copy-mode
        if path.startswith("/api/scroll/"):
            name = path.split("/api/scroll/")[1].strip("/")
            session = _session_name(name)
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            direction = data.get("direction", "")

            if direction not in ("up", "down"):
                self._send_json({"error": "invalid direction"}, 400)
                return

            subprocess.run(
                [TMUX_BIN, "copy-mode", "-t", session],
                capture_output=True
            )
            key = "PageUp" if direction == "up" else "PageDown"
            subprocess.run(
                [TMUX_BIN, "send-keys", "-t", session, key],
                capture_output=True
            )

            self._send_json({"ok": True})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


# ─── CLI ─────────────────────────────────────────────────────────────────────

def find_hub_pid() -> Optional[int]:
    """Find the PID of a running Codex Remote Hub server on HUB_PORT."""
    lsof = shutil.which("lsof")
    if lsof:
        try:
            out = subprocess.check_output(
                [lsof, "-ti", f":{HUB_PORT}"], text=True, stderr=subprocess.DEVNULL
            ).strip()
            if out:
                return int(out.split("\n")[0])
        except (subprocess.CalledProcessError, ValueError):
            pass

    ss = shutil.which("ss")
    if ss:
        try:
            out = subprocess.check_output(
                [ss, "-tlnpH", f"sport = :{HUB_PORT}"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            for line in out.split("\n"):
                if "pid=" in line:
                    for part in line.split(","):
                        if part.startswith("pid="):
                            return int(part.split("=")[1])
        except (subprocess.CalledProcessError, ValueError):
            pass

    return None


def cmd_stop():
    pid = find_hub_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        print(f"  Codex Remote Hub stopped (PID {pid})")
    else:
        print("  Codex Remote Hub is not running")
    pkill = shutil.which("pkill")
    if pkill:
        subprocess.run([pkill, "-f", "ttyd.*-p 78"], capture_output=True)


def cmd_status():
    pid = find_hub_pid()
    if pid:
        print(f"  Codex Remote Hub running (PID {pid}, port {HUB_PORT})")
        sessions = get_sessions()
        if sessions:
            for s in sessions:
                dot = "*" if s["has_ttyd"] else "o"
                print(f"   [{dot}] {s['name']} (port {s['port']}, {s['time']})")
        else:
            print("   No active sessions")
    else:
        print("  Codex Remote Hub is stopped")


def cmd_start():
    # Check dependencies before starting
    missing = _check_dependencies()
    if missing:
        print("  Missing required dependencies:")
        for name in missing:
            hint = _dependency_install_hint(name)
            print(f"    - {name}: {hint}")
        sys.exit(1)

    def cleanup(sig, frame):
        print("\n  Stopping Codex Remote Hub...")
        sessions = get_sessions()
        pkill = shutil.which("pkill")
        for s in sessions:
            port = s["port"]
            if pkill:
                subprocess.run([pkill, "-f", f"ttyd.*-p {port}"], capture_output=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    cert_file, key_file = _ssl_cert_paths()
    has_ssl = _has_ssl_certs()
    proto = "https" if has_ssl else "http"

    platform_label = PLATFORM
    if IS_WSL:
        platform_label = "wsl"

    print(f"""
  Codex Remote Hub v{VERSION} ({platform_label})

  {proto}://localhost:{HUB_PORT}
  Sessions use ports {BASE_PORT}-{MAX_PORT}
  {"HTTPS enabled" if has_ssl else "HTTPS not configured (optional)"}
  Press Ctrl+C to stop
""")

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer(("0.0.0.0", HUB_PORT), HubHandler)

    if has_ssl:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!MD5")
        ctx.options |= ssl.OP_NO_COMPRESSION | ssl.OP_CIPHER_SERVER_PREFERENCE
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    server.serve_forever()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"

    if cmd == "stop":
        cmd_stop()
    elif cmd == "restart":
        cmd_stop()
        time.sleep(1)
        cmd_start()
    elif cmd == "status":
        cmd_status()
    elif cmd == "start":
        cmd_start()
    elif cmd == "logs":
        os.execvp("tail", ["tail", "-f",
                           os.path.join(INSTALL_DIR, "hub.log"),
                           os.path.join(INSTALL_DIR, "hub-error.log")])
    else:
        print(f"Usage: codex-remote-hub.py {{start|stop|restart|status|logs}}")
        sys.exit(1)


if __name__ == "__main__":
    main()
