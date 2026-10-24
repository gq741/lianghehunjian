from mixstudio.desktop import main

if __name__ == "__main__":
    import os
    import sys
    try:
        main()
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else "启动失败，请通过工程内 Python 控制台命令检查依赖和运行环境。"
        if os.name == "nt" and "--headless" not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "量禾混剪", 0x10)
        elif sys.stderr:
            print(message, file=sys.stderr)
        raise SystemExit(1)
