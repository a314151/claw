import sys
from pathlib import Path

# 将 src 目录加入到 Python 搜索路径中，确保能够直接运行
src_path = str(Path(__file__).resolve().parent.parent)
if src_path not in sys.path:
    sys.path.append(src_path)

from personal_assistant.web_app import main

if __name__ == "__main__":
    main()
