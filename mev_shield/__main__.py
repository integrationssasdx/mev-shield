"""支持 ``python -m mev_shield``。"""

import sys

from .cli import run

if __name__ == "__main__":
    sys.exit(run())
