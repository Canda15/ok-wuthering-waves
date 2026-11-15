if __name__ == "__main__":
    from selfhost.user_data import bootstrap_user_data

    # 配置对象可能在导入期间立即读写，必须先保护并迁入旧用户数据。
    bootstrap_user_data()

    from config import config
    from ok import OK

    config = config
    config["debug"] = True
    config["gui"] = {
        "type": "web",
        "launch_mode": "pywebview",  # default
    }
    ok = OK(config)
    ok.start()
