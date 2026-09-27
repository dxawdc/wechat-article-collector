"""Prepare and start the bundled local collector, then run its conversation CLI."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import venv


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
CLIENT = ROOT / "scripts" / "collector_client.py"


def runtime_dir() -> Path:
    override = os.getenv("WEREAD_SKILL_RUNTIME_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        # Windows Store apps can redirect LOCALAPPDATA across volumes, which
        # breaks pip's atomic rename during installation.
        base = Path.home() / ".cache"
    else:
        base = Path(os.getenv("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "wechat-article-collector" / "runtime"


def data_dir() -> Path:
    return Path(os.getenv("WEREAD_SKILL_DATA_DIR") or (Path.home() / ".wechat-article-collector"))


def runtime_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_runtime() -> Path:
    if sys.version_info < (3, 11):
        raise RuntimeError("此 skill 需要 Python 3.11 或更新版本")
    target = runtime_dir()
    python = runtime_python(target)
    marker = target / ".requirements-sha256"
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if python.is_file() and marker.is_file() and marker.read_text(encoding="ascii") == digest:
        ensure_browser(python, target)
        return python
    target.parent.mkdir(parents=True, exist_ok=True)
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(target)
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
         "-r", str(REQUIREMENTS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "安装 skill 依赖失败：" + (result.stderr or result.stdout)[-800:] +
            "\n可手动补救：用同一 runtime 的 pip 安装 requirements.txt（国内建议加镜像 "
            "-i https://mirrors.tencent.com/pypi/simple/），再写入 .requirements-sha256 标记。")
    marker.write_text(digest, encoding="ascii")
    ensure_browser(python, target)
    return python


def ensure_browser(python: Path, runtime: Path) -> None:
    if runtime.joinpath(".browser-ready").is_file():
        return
    names = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")
    if any(shutil.which(name) for name in names):
        return
    if os.name == "nt":
        roots = [os.getenv("PROGRAMFILES", ""), os.getenv("PROGRAMFILES(X86)", ""), os.getenv("LOCALAPPDATA", "")]
        if any((Path(root) / company / product / executable).is_file()
               for root in roots if root
               for company, product, executable in (("Google", "Chrome/Application", "chrome.exe"),
                                                     ("Microsoft", "Edge/Application", "msedge.exe"))):
            return
    elif sys.platform == "darwin":
        if any(Path(path).is_file() for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")):
            return
    result = subprocess.run([str(python), "-m", "playwright", "install", "chromium"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=600)
    if result.returncode:
        raise RuntimeError("安装采集所需浏览器失败：" + (result.stderr or result.stdout)[-800:])
    runtime.joinpath(".browser-ready").write_text("ok", encoding="ascii")


def _service_healthy(port: int, token: str) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            data = json.loads(response.read(1024))
        if data.get("service") != "wechat-article-collector":
            return False
        request = Request(f"http://127.0.0.1:{port}/api/collector/accounts",
                          headers={"X-Collector-Key": token})
        with urlopen(request, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _token(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    path = root / "local-token"
    if not path.is_file():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as target:
                target.write(secrets.token_urlsafe(32))
    if os.name != "nt":
        path.chmod(0o600)
    return path.read_text(encoding="ascii").strip()


def ensure_service(python: Path) -> tuple[str, str]:
    root = data_dir()
    token = _token(root)
    state_file = root / "service.json"
    if state_file.is_file():
        try:
            current = json.loads(state_file.read_text(encoding="utf-8"))
            port = int(current["port"])
            if _service_healthy(port, token):
                return f"http://127.0.0.1:{port}", token
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log_file = (root / "service.log").open("ab")
    environment = {**os.environ, "WEREAD_SKILL_DATA_DIR": str(root),
                   "WEREAD_SKILL_BROWSER_ADMIN_URL": f"http://127.0.0.1:{port}/verify"}
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            [str(python), "-m", "uvicorn", "service.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log"],
            cwd=str(ROOT), env=environment, stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=subprocess.STDOUT, creationflags=flags,
            start_new_session=os.name != "nt",
        )
    finally:
        log_file.close()
    for _ in range(80):
        if _service_healthy(port, token):
            state_file.write_text(json.dumps({"port": port, "pid": process.pid}), encoding="utf-8")
            return f"http://127.0.0.1:{port}", token
        if process.poll() is not None:
            break
        time.sleep(0.25)
    raise RuntimeError(f"本地采集服务启动失败；请检查 {root / 'service.log'}")


def ensure_login_start() -> str:
    """Keep Windows scheduled collection running after the next sign-in."""
    if os.name != "nt":
        return "此系统尚未自动配置登录自启；周期采集需要本地服务持续运行"
    command = f'"{sys.executable}" "{Path(__file__).resolve()}" doctor'
    try:
        result = subprocess.run(
            ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
             "/TN", "WeRead Article Collector", "/TR", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Windows 登录自启设置失败：{str(exc)[:180]}"
    if result.returncode:
        return "Windows 登录自启设置失败；当前登录会话中的服务仍会执行定时任务"
    return ""


def main() -> int:
    try:
        python = ensure_runtime()
        if sys.argv[1:] in (["--help"], ["-h"]):
            return subprocess.call([str(python), str(CLIENT), *sys.argv[1:]])
        url, token = ensure_service(python)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    env = {**os.environ, "WEREAD_SKILL_SERVICE_URL": url,
           "WEREAD_SKILL_SERVICE_TOKEN": token,
           "WEREAD_SKILL_DATA_DIR": str(data_dir())}
    result = subprocess.run([str(python), str(CLIENT), *sys.argv[1:]], env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if "schedule" in sys.argv[1:] and result.returncode == 0:
        try:
            response = json.loads(result.stdout)
            if response.get("status") == "scheduled":
                warning = ensure_login_start()
                if warning:
                    response["autostartWarning"] = warning
                print(json.dumps(response, ensure_ascii=True, indent=2))
                return 0
        except (ValueError, TypeError):
            pass
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
