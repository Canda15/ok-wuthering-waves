if __name__ == '__main__':
    from selfhost.user_data import bootstrap_user_data

    # 配置对象可能在导入期间立即读写，必须先保护并迁入旧用户数据。
    bootstrap_user_data()

    from config import config
    from ok import OK
    from selfhost.overrides import apply as apply_selfhost_overrides

    # 与 main.py 保持一致:自有差异必须在 OK() 之前写入 config。
    apply_selfhost_overrides(config)

    config = config
    config['debug'] = True
    # config['click_screenshots_folder'] = "click_screenshots"  # debug用 点击后截图文件夹]
    ok = OK(config)
    ok.start()
