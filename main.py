if __name__ == '__main__':
    from config import config
    from ok import OK
    from selfhost.overrides import apply as apply_selfhost_overrides

    # 自有差异(恢复上游摘掉的自定义页面)必须在 OK() 之前写入:
    # OK.__init__ 按引用保存 config,MainWindow 读的是同一个字典。
    # 放在这里的而不是 config.py,是为了让上游改 config.py 时不再和我们冲突。
    # 见 selfhost/overrides.py 与 SELFHOST.md。
    apply_selfhost_overrides(config)

    config = config
    ok = OK(config)
    ok.start()
