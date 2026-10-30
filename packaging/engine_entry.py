"""独立后端运行时入口，兼容原有命令与桌面部署命令。"""

import sys

from second_proprio import cli
from second_proprio import desktop_deployment

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "desktop-control":
        desktop_deployment.main(sys.argv[2:])
    else:
        cli.main()
