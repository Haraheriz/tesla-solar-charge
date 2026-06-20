import os


def load_env(filename=".env"):
    """poc/.env (gitignore対象) を読み込み、未設定の環境変数にのみ反映する。"""
    path = os.path.join(os.path.dirname(__file__), filename)
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
