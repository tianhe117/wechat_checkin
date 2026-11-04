"""Single-process manager entry point for Windows, Linux venv and Docker."""
import argparse
from contextlib import contextmanager
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import signal

from waitress import create_server
from . import PROJECT_ROOT
from .web import create_app


@contextmanager
def instance_lock(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory/"service.lock").open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("同一数据目录已有服务运行，请勿重复启动") from None
    try:
        yield
    finally:
        handle.close()


def main():
    parser = argparse.ArgumentParser(description="企业微信自动任务工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=str(PROJECT_ROOT/"data"))
    parser.add_argument("--http", "--dev", dest="http", action="store_true", help="直接通过 HTTP 访问时使用；HTTPS 反代部署不使用此项")
    parser.add_argument("--trusted-proxy", default="127.0.0.1", help="Nginx 来源 IP；隔离的 Docker 网络可填 *")
    args = parser.parse_args()
    with instance_lock(args.data_dir):
        handler = RotatingFileHandler(Path(args.data_dir)/"service.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[logging.StreamHandler(), handler])
        app = create_app(args.data_dir, secure_cookie=not args.http)
        service = app.extensions["service"]
        server = create_server(app, host=args.host, port=args.port, threads=4, trusted_proxy=args.trusted_proxy,
                               trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"})

        def terminate(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
        try:
            service.start()
            logging.info("Manager listening on %s:%s", args.host, args.port)
            server.run()
        except KeyboardInterrupt:
            pass
        finally:
            server.close()
            service.stop()


if __name__ == "__main__":
    main()
