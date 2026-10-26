`packaging/build_service.py` stages the frozen BURNRATE service here before a
desktop bundle is built. Only this file is tracked; with nothing else here the
desktop app falls back to a checkout's `.venv`.
