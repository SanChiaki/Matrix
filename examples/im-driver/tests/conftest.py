import sys
from pathlib import Path

# 没装成包时（在 Matrix 仓库里直接跑 pytest）也能 import matrix_im_demo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
