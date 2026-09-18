"""集中 logging 設定——rotating file handler,讓 dev 能快速找到失敗原因。

GUI 用 pythonw.exe 執行(無 console),native host 的 stdout 被 native
messaging 二進位協定佔用(見 native_host._write_message,貿然 print 會汙染
IPC 流)——兩者都無法靠 print/stderr 診斷失敗,唯一可靠的管道是寫檔案。

GUI 與 native host 是不同 process,但共用同一個 log 檔(app_dir()/logs/
pwmgr.log),用 component 參數在每行標明來源方便分辨。兩個進入點
(pwmgr/__main__.py)各自呼叫一次 setup_logging()。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading

from .config import LOG_BACKUP_COUNT, LOG_FILE_MAX_BYTES, logs_dir

_configured = False


def setup_logging(component: str) -> logging.Logger:
    """設定 "pwmgr" logger 寫到 rotating file,並接管未捕捉例外。

    重複呼叫是 no-op(回傳已設定好的 logger)。
    """
    global _configured
    logger = logging.getLogger("pwmgr")
    if _configured:
        return logger

    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        logs_dir() / "pwmgr.log",
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            f"%(asctime)s [%(levelname)s] [{component}] %(name)s: %(message)s"
        )
    )
    logger.addHandler(handler)
    logger.propagate = False

    sys.excepthook = _make_excepthook(logger)
    threading.excepthook = _make_thread_excepthook(logger)

    _configured = True
    logger.info("===== 啟動 (component=%s) =====", component)
    return logger


def _make_excepthook(logger: logging.Logger):
    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("未捕捉例外(主 thread)", exc_info=(exc_type, exc_value, exc_tb))

    return _hook


def _make_thread_excepthook(logger: logging.Logger):
    def _hook(args: threading.ExceptHookArgs) -> None:
        thread_name = args.thread.name if args.thread is not None else "?"
        logger.critical(
            "未捕捉例外(背景 thread=%s)",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    return _hook
