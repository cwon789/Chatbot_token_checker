# Windows 에서 더블클릭(콘솔 없이)으로 실행하기 위한 입구. 본체는 ccusage_widget.py 하나다.
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccusage_widget.py"),
               run_name="__main__")
