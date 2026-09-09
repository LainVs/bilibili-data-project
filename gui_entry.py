"""打包用 GUI 入口：等价于 `python main.py gui`。"""
from crawler.logging_setup import setup_logging
from gui.main_window import run

if __name__ == "__main__":
    setup_logging()
    run()
