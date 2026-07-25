"""Windows desktop launcher for the local web application."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from contextlib import suppress
from pathlib import Path
from tkinter import LEFT, Button, Frame, Label, StringVar, Tk, X, messagebox

import psutil

APP_NAME = "DocQA"
LEGACY_APP_NAME = "EnterpriseDocumentRAG"
INSTANCE_FILE_NAME = "desktop-service.json"
CONTROL_TOKEN_ENV = "DOCQA_DESKTOP_CONTROL_TOKEN"
RECOVER_LEASES_ENV = "DOCQA_RECOVER_LEASES"


def _application_home() -> Path:
    bundle = _bundle_home()
    if (bundle / "portable.mode").is_file():
        home = bundle / "user-data"
    else:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        preferred_home = base / APP_NAME
        legacy_home = base / LEGACY_APP_NAME
        home = (
            legacy_home
            if legacy_home.is_dir() and not preferred_home.exists()
            else preferred_home
        )
    for child in ("data", "knowledge", "models/huggingface"):
        (home / child).mkdir(parents=True, exist_ok=True)
    return home


def _bundle_home() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _configure_environment(home: Path) -> None:
    bundle = _bundle_home()
    # The desktop batch starts this launcher from the source checkout.  Keep
    # that development configuration (notably HUGGINGFACE_HOME) intact so an
    # already downloaded answer model is reused.  Packaged applications still
    # receive their isolated per-user configuration below.
    if not getattr(sys, "frozen", False) and (bundle / ".env").is_file():
        os.environ.setdefault("APP_ENV", "development")
        os.chdir(bundle)
        return
    bundled_models = bundle / "models" / "huggingface"
    model_home = bundled_models if bundled_models.is_dir() else home / "models" / "huggingface"
    os.environ.setdefault("APP_ENV", "production")
    os.environ.setdefault("APP_DATA_DIR", str(home))
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{(home / 'data' / 'agent.db').as_posix()}")
    os.environ.setdefault("QDRANT_PATH", str(home / "data" / "qdrant"))
    os.environ.setdefault("AUTHORIZED_ROOTS", str(home / "knowledge"))
    os.environ.setdefault("HUGGINGFACE_HOME", str(model_home))
    offline_marker = bundle / "offline.mode"
    online_models_marker = bundle / "online-models.mode"
    embedding_model = bundle / "models" / "embedding-bge-small-zh-v1.5"
    reranker_model = bundle / "models" / "reranker-bge-base-int8"
    qwen_model = bundle / "models" / "qwen3" / "Qwen3-0.6B-Q8_0.gguf"
    llama_cli = bundle / "tools" / "llama.cpp" / "llama-cli.exe"
    bsdtar = bundle / "tools" / "libarchive" / "bsdtar.exe"
    # Prefer the current Transformers package when an older GGUF installation
    # still has a stale offline.mode marker after an update.
    if online_models_marker.is_file():
        required = (embedding_model,)
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise RuntimeError(
                "Online model package is missing bundled model assets:\n" + "\n".join(missing)
            )
        os.environ.setdefault("EMBEDDING_MODEL", str(embedding_model))
        os.environ["RERANKER_ENABLED"] = "false"
        os.environ.setdefault("LLM_BACKEND", "qwen_transformers")
        os.environ.setdefault("LLM_MODEL_ID", "Qwen/Qwen3-0.6B")
        # Keep packaged and development defaults aligned: model download remains
        # an explicit user action from the application UI.
        os.environ.setdefault("MODEL_AUTO_DOWNLOAD", "false")
    elif offline_marker.is_file():
        required = (embedding_model, reranker_model, qwen_model, llama_cli, bsdtar)
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise RuntimeError("离线完整版缺少必要资产：\n" + "\n".join(missing))
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("EMBEDDING_MODEL", str(embedding_model))
        os.environ["RERANKER_ENABLED"] = "false"
        os.environ.setdefault("LLM_BACKEND", "qwen_gguf_cli")
        os.environ.setdefault("LLM_MODEL_ID", str(qwen_model))
        os.environ.setdefault("LLAMA_CLI_PATH", str(llama_cli))
        os.environ.setdefault("BSDTAR_PATH", str(bsdtar))
    os.chdir(home)


def _available_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("没有可用的本地端口（已检查 8765-8784）")


def _read_instance(path: Path) -> dict[str, object] | None:
    """Read a launcher descriptor without trusting its contents."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _is_same_desktop_service(process: psutil.Process, descriptor: dict[str, object]) -> bool:
    """Ensure a reused PID can never cause an unrelated process to be stopped."""
    expected_executable = descriptor.get("executable")
    if not isinstance(expected_executable, str) or not expected_executable:
        return False
    try:
        actual_executable = Path(process.exe()).resolve()
        return actual_executable == Path(expected_executable).resolve()
    except (OSError, psutil.Error):
        return False


def _request_graceful_shutdown(descriptor: dict[str, object]) -> bool:
    port = descriptor.get("port")
    token = descriptor.get("control_token")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    if not isinstance(token, str) or not token:
        return False
    query = urllib.parse.urlencode({"token": token})
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/internal/desktop/stop?{query}",
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:  # nosec B310: loopback only
            return 200 <= response.status < 300
    except OSError:
        return False


def _wait_for_exit(process: psutil.Process, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process.is_running():
            return True
        time.sleep(0.1)
    return not process.is_running()


def _stop_verified_service(process: psutil.Process, descriptor: dict[str, object]) -> bool:
    """Ask the old FastAPI app to close first, then use a bounded fallback."""
    _request_graceful_shutdown(descriptor)
    if _wait_for_exit(process, timeout_seconds=6):
        return True
    try:
        process.terminate()
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False
    if _wait_for_exit(process, timeout_seconds=4):
        return True
    try:
        process.kill()
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False
    return _wait_for_exit(process, timeout_seconds=2)


def _legacy_service_processes() -> list[psutil.Process]:
    """Find pre-descriptor development services, without matching generic Python jobs."""
    current_pid = os.getpid()
    current_executable = Path(sys.executable).resolve()
    packaged = bool(getattr(sys, "frozen", False))
    found: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        if process.info["pid"] == current_pid:
            continue
        command_line = " ".join(process.info.get("cmdline") or ())
        is_legacy_uvicorn = "enterprise_document_rag.main:app" in command_line
        try:
            is_same_packaged_launcher = (
                packaged and Path(process.exe()).resolve() == current_executable
            )
        except (OSError, psutil.Error):
            is_same_packaged_launcher = False
        if is_legacy_uvicorn or is_same_packaged_launcher:
            found.append(process)
    return found


class DesktopLauncher:
    def __init__(self) -> None:
        self.home = _application_home()
        self.log_file = (self.home / "launcher.log").open(
            "a", encoding="utf-8", buffering=1
        )
        if sys.stdout is None:
            sys.stdout = self.log_file
        if sys.stderr is None:
            sys.stderr = self.log_file
        self.instance_path = self.home / INSTANCE_FILE_NAME
        self.control_token = secrets.token_urlsafe(32)
        recovered_service = self._reclaim_stale_service()
        if recovered_service:
            # The new server only releases leases after this verified hand-off.
            # This avoids releasing leases while a separate, healthy service owns them.
            os.environ[RECOVER_LEASES_ENV] = "1"
        else:
            os.environ.pop(RECOVER_LEASES_ENV, None)
        os.environ[CONTROL_TOKEN_ENV] = self.control_token
        _configure_environment(self.home)
        self.port = _available_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = None
        self.error: BaseException | None = None

        self.root = Tk()
        self.root.title("Document RAG")
        icon_path = _bundle_home() / "docqa.ico"
        if icon_path.is_file():
            with suppress(Exception):
                self.root.iconbitmap(default=str(icon_path))
        self.root.geometry("520x230")
        self.root.minsize(480, 210)
        self.root.protocol("WM_DELETE_WINDOW", self.stop)

        self.status = StringVar(value="正在启动本地服务，请稍候……")
        Label(self.root, text="Document RAG", font=("Microsoft YaHei UI", 16, "bold")).pack(
            pady=(24, 10)
        )
        Label(self.root, textvariable=self.status, font=("Microsoft YaHei UI", 10)).pack(
            padx=20, pady=8
        )
        Label(
            self.root,
            text=f"数据保存在：{self.home}",
            font=("Microsoft YaHei UI", 9),
            wraplength=470,
        ).pack(padx=20, pady=4)

        buttons = Frame(self.root)
        buttons.pack(fill=X, padx=40, pady=18)
        self.open_button = Button(
            buttons, text="打开应用", command=self.open_browser, state="disabled", width=14
        )
        self.open_button.pack(side=LEFT, expand=True)
        Button(buttons, text="打开数据目录", command=self.open_data_folder, width=14).pack(
            side=LEFT, expand=True
        )
        Button(buttons, text="退出", command=self.stop, width=10).pack(side=LEFT, expand=True)

    def start(self) -> None:
        threading.Thread(target=self._run_server, name="local-web-server", daemon=True).start()
        self.root.after(200, self._check_server)
        self.root.mainloop()

    def _run_server(self) -> None:
        try:
            import uvicorn

            from enterprise_document_rag.main import create_app

            app = create_app()
            app.state.shutdown_callback = self._shutdown_for_update
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                log_config=None,
                access_log=False,
            )
            self.server = uvicorn.Server(config)
            self._write_instance_descriptor()
            self.server.run()
        except BaseException as exc:  # surfaced in the launcher window
            self.error = exc
        finally:
            self._remove_own_instance_descriptor()

    def _reclaim_stale_service(self) -> bool:
        """Release a previous DocQA process before it can compete for SQLite."""
        recovered = False
        descriptor = _read_instance(self.instance_path)
        if descriptor is None and self.instance_path.exists():
            # A partially written descriptor cannot identify a live process;
            # legacy process detection below still protects against one.
            self.instance_path.unlink(missing_ok=True)
            recovered = True
        elif descriptor is not None:
            pid = descriptor.get("pid")
            try:
                process = psutil.Process(pid) if isinstance(pid, int) else None
            except psutil.NoSuchProcess:
                process = None
            except psutil.Error as exc:
                raise RuntimeError("Unable to inspect the previous DocQA service safely.") from exc
            if process is None:
                self.instance_path.unlink(missing_ok=True)
                recovered = True
            elif _is_same_desktop_service(process, descriptor):
                if not _stop_verified_service(process, descriptor):
                    raise RuntimeError("Unable to stop the previous DocQA service safely.")
                self.instance_path.unlink(missing_ok=True)
                recovered = True
            else:
                # The PID was reused. Never terminate a process that cannot be proven to
                # be the old DocQA instance.
                self.instance_path.unlink(missing_ok=True)

        # Releases made before service descriptors existed are Python/Uvicorn
        # processes with this exact application module in their command line.
        for process in _legacy_service_processes():
            if not _stop_verified_service(process, {}):
                raise RuntimeError("Unable to stop the previous Python DocQA service safely.")
            recovered = True
        return recovered

    def _write_instance_descriptor(self) -> None:
        descriptor = {
            "pid": os.getpid(),
            "port": self.port,
            "control_token": self.control_token,
            "executable": str(Path(sys.executable).resolve()),
        }
        temporary = self.instance_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(descriptor, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, self.instance_path)

    def _remove_own_instance_descriptor(self) -> None:
        descriptor = _read_instance(self.instance_path)
        if descriptor is not None and descriptor.get("pid") == os.getpid():
            self.instance_path.unlink(missing_ok=True)

    def _check_server(self) -> None:
        if self.error is not None:
            self.status.set("启动失败")
            messagebox.showerror("启动失败", f"本地服务无法启动：\n\n{self.error}")
            return
        if self.server is not None and self.server.started:
            self.status.set("本地服务已启动。关闭此窗口将退出应用。")
            self.open_button.config(state="normal")
            self.open_browser()
            return
        self.root.after(200, self._check_server)

    def open_browser(self) -> None:
        webbrowser.open(self.url)

    def open_data_folder(self) -> None:
        os.startfile(self.home)  # type: ignore[attr-defined]

    def stop(self) -> None:
        if self.server is not None:
            self.status.set("正在退出……")
            self.server.should_exit = True
        self.root.after(250, self.root.destroy)

    def _shutdown_for_update(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        self.root.after(0, self.root.destroy)


def main() -> None:
    DesktopLauncher().start()


if __name__ == "__main__":
    main()
