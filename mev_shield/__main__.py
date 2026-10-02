"""命令行入口：python -m mev_shield --input IN --output OUT"""

import sys

from .core import main

if __name__ == "__main__":
    main(sys.argv[1:])
