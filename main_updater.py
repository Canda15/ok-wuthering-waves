"""为当前源码目录打开独立更新管理器，便携版使用原生启动器的更新界面。"""

if __name__ == '__main__':
    import sys
    from pathlib import Path

    from pyappify.updater_ui import main

    sys.exit(main(['--repo', str(Path(__file__).resolve().parent), *sys.argv[1:]]))
