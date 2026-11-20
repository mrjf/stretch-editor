"""The tweet preview: xeet (X's post layout rebuilt in WebAssembly) and the author's X profile.

xeet is fetched once from the npm registry at a pinned version, checked against
the registry's published sha512, and unpacked under the app's data folder in a
directory the backup skips (it can always be fetched again). The browser loads
it from this app's own origin.

The profile comes from an X connection in the studio's connected accounts, when
there is one; otherwise the preview uses placeholders the author can edit.
"""

import base64
import hashlib
import io
import json
import re
import subprocess
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

XEET_VERSION = "0.1.2"
XEET_TARBALL = f"https://registry.npmjs.org/@xeet/core/-/core-{XEET_VERSION}.tgz"
XEET_INTEGRITY = "sha512-t2LSqZVRM6jLcX0Znjy5IldJTLluXAUcj58pv8lA0x0vaW9jwwHzS0sxZ8lEqSg5lI/xgoW842bn4z2BgNDUAQ=="
X_SERVICE_NAMES = ("x", "twitter", "custom_https_x_com", "custom_https_twitter_com", "custom_https_api_x_com")

_lock = threading.Lock()


def xeet_dir(data_dir: Path) -> Path:
    return data_dir / "vendor" / f"xeet-core-{XEET_VERSION}"


def ensure_xeet(data_dir: Path) -> Path | None:
    """Unpack the pinned xeet package if it is not there yet. Returns its folder, or None on failure."""
    target = xeet_dir(data_dir)
    with _lock:
        if (target / "package" / "wasm" / "xeet_wasm_bg.wasm").is_file():
            return target
        try:
            with urllib.request.urlopen(XEET_TARBALL, timeout=120) as response:
                blob = response.read()
            digest = "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()
            if digest != XEET_INTEGRITY:
                logger.error("xeet download did not match its published fingerprint; not using it")
                return None
            target.parent.mkdir(parents=True, exist_ok=True)
            (target.parent / ".nobackup").touch()
            tmp = target.with_suffix(".partial")
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
                for member in archive.getmembers():
                    name = Path(member.name)
                    if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                        continue
                    archive.extract(member, tmp, filter="data")
            tmp.rename(target)
            logger.info("xeet {} ready at {}", XEET_VERSION, target)
            return target
        except (OSError, tarfile.TarError) as exc:
            logger.error("Could not fetch xeet: {}", exc)
            return None


ESM_NAME = "xeet_wasm.esm.js"


def browser_loader(package: Path) -> Path:
    """The package ships its WebAssembly loader in the server-side form (it reads the .wasm file from disk).
    Rewrite it once into a browser module: same functions, exported, plus an ``init(url)`` that fetches the
    .wasm over the network."""
    target = package / "wasm" / ESM_NAME
    if target.is_file():
        return target
    source = (package / "wasm" / "xeet_wasm.js").read_text()
    names = re.findall(r"^exports\.(\w+) = \1;$", source, flags=re.M)
    body = re.sub(r"^exports\.(\w+) = \1;$", "", source, flags=re.M)
    start = body.index("const wasmPath = require(")
    body = body[:start] + (
        "let wasm;\n"
        "export default async function init(url) {\n"
        "  const { instance } = await WebAssembly.instantiateStreaming(fetch(url), __wbg_get_imports());\n"
        "  wasm = instance.exports;\n"
        "  wasm.__wbindgen_start();\n"
        "}\n"
        f"export {{ {', '.join(names)} }};\n"
    )
    if "require(" in body:
        raise ValueError("unexpected server-only code left in the xeet loader")
    target.write_text(body)
    return target


def xeet_file(data_dir: Path, filepath: str) -> Path | None:
    """A file the browser may load from the package: its scripts, WebAssembly and font packs."""
    allowed = filepath.startswith(("src/", "wasm/")) and filepath.endswith((".js", ".wasm", ".bin", ".json"))
    if not allowed or ".." in Path(filepath).parts:
        return None
    root = ensure_xeet(data_dir)
    if root is None:
        return None
    if filepath == f"wasm/{ESM_NAME}":
        try:
            return browser_loader(root / "package").absolute()
        except (OSError, ValueError) as exc:
            logger.error("Could not prepare the xeet loader for the browser: {}", exc)
            return None
    path = (root / "package" / filepath).absolute().resolve()
    return path if path.is_file() and (root / "package").absolute().resolve() in path.parents else None


class XProfile(FrozenModel):
    """Who the tweet preview is drawn as."""

    connected: bool = Field(description="Whether an X account is connected in the studio")
    name: str = Field(description="Display name")
    handle: str = Field(description="Handle without the @")
    verified: str = Field(default="none", description="none, blue, business or government")
    avatar: str | None = Field(default=None, description="Profile picture as base64 image bytes")


class HandleProfile(FrozenModel):
    """The public X profile behind a handle."""

    found: bool = Field(description="Whether the handle was found")
    name: str = Field(default="", description="Display name")
    handle: str = Field(description="Handle without the @")
    verified: str = Field(default="none", description="none, blue, business or government")
    avatar: str | None = Field(default=None, description="Profile picture as base64 image bytes")


_handle_cache: dict[str, tuple[float, HandleProfile]] = {}


def _get(url: str, timeout: float = 15) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "stretch-editor/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def profile_for_handle(handle: str) -> HandleProfile:
    """Look up a public X profile by handle (no sign-in needed): name, picture and badge.

    Uses the public FxTwitter API, falling back to unavatar.io for just the picture. Cached for ten minutes.
    """
    handle = handle.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return HandleProfile(found=False, handle=handle)
    cached = _handle_cache.get(handle.lower())
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    name, verified, avatar_url = "", "none", ""
    try:
        user = json.loads(_get(f"https://api.fxtwitter.com/{handle}")).get("user") or {}
        name = str(user.get("name") or "")
        handle = str(user.get("screen_name") or handle)
        avatar_url = str(user.get("avatar_url") or "").replace("_normal.", "_400x400.")
        badge = user.get("verification") or {}
        if badge.get("verified"):
            verified = {"business": "business", "government": "government"}.get(str(badge.get("type")), "blue")
    except (OSError, ValueError):
        user = {}
    avatar = None
    for url in [avatar_url, f"https://unavatar.io/x/{handle}?fallback=false"]:
        if not url.startswith("https://"):
            continue
        try:
            avatar = base64.b64encode(_get(url)).decode()
            break
        except OSError:
            continue
    profile = HandleProfile(found=bool(user or avatar), name=name, handle=handle, verified=verified, avatar=avatar)
    _handle_cache[handle.lower()] = (time.time(), profile)
    return profile


PLACEHOLDER = XProfile(connected=False, name="Your Name", handle="yourhandle")


def x_profile() -> XProfile:
    """The connected X account's name, handle and picture, or placeholders when there is none."""
    try:
        listed = subprocess.run(["latchkey", "auth", "list"], capture_output=True, text=True, timeout=20, check=False)
        services = json.loads(listed.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return PLACEHOLDER
    service = next((s for s in X_SERVICE_NAMES if s in services), None)
    if not service:
        return PLACEHOLDER
    try:
        me = subprocess.run(
            ["latchkey", "curl", "-s",
             "https://api.x.com/2/users/me?user.fields=name,username,profile_image_url,verified_type"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        user = json.loads(me.stdout).get("data") or {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return PLACEHOLDER.model_copy(update={"connected": True})
    avatar = None
    url = str(user.get("profile_image_url") or "").replace("_normal.", "_400x400.")
    if url.startswith("https://"):
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                avatar = base64.b64encode(response.read()).decode()
        except OSError:
            avatar = None
    verified = {"blue": "blue", "business": "business", "government": "government"}.get(str(user.get("verified_type")), "none")
    return XProfile(
        connected=True,
        name=str(user.get("name") or PLACEHOLDER.name),
        handle=str(user.get("username") or PLACEHOLDER.handle),
        verified=verified,
        avatar=avatar,
    )
