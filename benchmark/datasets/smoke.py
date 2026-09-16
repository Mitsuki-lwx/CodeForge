"""CodeForge 冒烟基准数据集。

每条 item 都是一个自包含的编程/问答任务：跑在独立临时 cwd 上，不依赖仓库状态。
`seed_files` 会在跑 CodeForge 之前写入临时 cwd；任务文本可引用这些文件名。

item 字段：
  name          唯一标识（用于 dataset item 名 / 打分关联）
  task          喂给 CodeForge Agent 的指令（可引用临时 cwd 里的文件）
  metric        打分方式：contains / exact / regex / pytest_pass
  expected_output  contains/exact/regex 的真值
  regex            metric=regex 时的正则（含 expected_output 时优先用 expected_output 匹配）
  seed_files       dict[文件名 -> 内容]，跑前写入临时 cwd
"""

from __future__ import annotations

# 记录真实耗时用；数据集本身是同质的。
GOAL = "修正 add() 使其返回 a+b"

SMOKE: list[dict] = [
    # ── 1. 修 bug + 跑测试（验证真实行为，最强指标）──────────────────
    {
        "name": "add_bugfix",
        "task": (
            "项目在临时目录里。文件 calc.py 的 add(a, b) 目前返回 a-b（有 bug，应返回 a+b）。"
            "请读 calc.py，修复 add()，然后运行 pytest 让 test_calc.py 全部通过。"
            "做完用一句话总结你改了哪个文件、测试是否通过。"
        ),
        "metric": "pytest_pass",
        "expected_output": "",
        "cases": [
            {"input": "", "want": "", "kind": "lines", "expect_error": False},
        ],
        "steps": [
            # 强指标：pytest 全过才算真正修好（hard 硬门）
            {"name": "bugfixed", "check": "pytest_pass", "weight": 3, "hard": True},
            # 弱指标：总结提到改动/测试结果（连续 = 命中比例）
            {
                "name": "has_summary",
                "check": "contains",
                "contains": ["改了", "通过", "修复"],
                "weight": 1,
            },
        ],
        "step_gates": [{"when": "bugfixed", "thresh": 1.0}],
        "seed_files": {
            "calc.py": (
                "def add(a, b):\n"
                "    return a - b  # bug: should be a + b\n"
                "\n"
                "def mul(a, b):\n"
                "    return a * b\n"
            ),
            "test_calc.py": (
                "from calc import add, mul\n"
                "\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n"
                "\n"
                "def test_mul():\n"
                "    assert mul(2, 3) == 6\n"
            ),
            "__init__.py": "",
        },
    },
    # ── 2. 写单元测试（验证行为：pytest 通过）──────────────────────
    {
        "name": "write_unit_tests",
        "task": (
            "临时目录里有 calc.py（含 add 与 mul 两个正确函数），但没有测试文件。"
            "请为 add(a,b) 和 mul(a,b) 各写至少一个正确的 pytest 测试（文件 test_calc.py），"
            "然后运行 pytest 并确保全部通过。做完用一句话总结。"
        ),
        "metric": "pytest_pass",
        "expected_output": "",
        "cases": [
            {"input": "", "want": "", "kind": "lines", "expect_error": False},
        ],
        "seed_files": {
            "calc.py": (
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
            ),
            "__init__.py": "",
        },
    },
    # ── 3. 写函数并输出结果（contains 校验输出里的答案）────────────────
    {
        "name": "write_fib",
        "task": (
            "在临时目录新建 fib.py，写一个函数 fib(n) 返回第 n 个斐波那契数（fib(0)=0, fib(1)=1）。"
            "然后用 print(fib(10)) 打印 fib(10) 的值并运行它。做完用一句话总结你得到的结果。"
        ),
        "metric": "contains",
        "expected_output": "55",
        "cases": [
            {"input": "", "want": "55", "kind": "number≈"},
        ],
        "steps": [
            # 强指标：确实创建了 fib.py 且含函数定义（连续 = 锚点命中）
            {
                "name": "fib_impl",
                "check": "file_contains",
                "path": "fib.py",
                "needles": ["def fib"],
                "weight": 2,
            },
            # 输出对：打印的 fib(10) 必须是 55
            {
                "name": "prints_result",
                "check": "contains",
                "contains": ["55"],
                "weight": 2,
            },
        ],
        "seed_files": {"__init__.py": ""},
    },
    # ── 4. 纯函数任务（contains 校验输出）────────────────────────────
    {
        "name": "reverse_string",
        "task": (
            "写一个 Python 函数 reverse(s) 返回字符串 s 的逆序。"
            '用 print(reverse("abc")) 打印 reverse("abc") 的结果并运行它。'
            "做完用一句话总结你得到的结果。"
        ),
        "metric": "contains",
        "expected_output": "cba",
        "cases": [
            {"input": "", "want": "cba", "kind": "lines"},
        ],
        "seed_files": {"__init__.py": ""},
    },
    # ── 5. 纯算法任务（contains 校验 output 里的质数）────────────────
    {
        "name": "list_primes_under_20",
        "task": (
            "写一个小脚本：找出所有小于 20 的质数，用 print 打印成一行。"
            "运行它。做完用一句话总结你打印了哪些质数。"
        ),
        "metric": "contains",
        "expected_output": "19",
        "cases": [
            {"input": "", "want": "2 3 5 7 11 13 17 19", "kind": "set"},
        ],
        "seed_files": {"__init__.py": ""},
    },
    # ── 6. 概念问答（regex 校验必须同时提到 商 和 余数）────────────
    {
        "name": "explain_quotient_remainder",
        "task": (
            "请用一两句话解释 Python 里 //（整除）与 %（取余）的区别。"
            "回答时必须提到「商」和「余数」这两个词。"
        ),
        "metric": "regex",
        "expected_output": "",
        "regex": r"(商.*余数|余数.*商)",
        "cases": [],
        "seed_files": {},
    },
    # ── 7. 语义等价：城市间距离（黑盒执行，靠数值归约，非子串命中）────
    {
        "name": "city_distance",
        "task": (
            "在临时目录新建 distance.py，写一个函数 haversine(lat1, lon1, lat2, lon2) "
            "返回两个(纬度,经度)点间的大圆距离（公里）。程序从 stdin 读一行 "
            "`lat1 lon1 lat2 lon2`，输出距离（可带小数）。用 print 打印即可。"
            "做完用一句话总结你得到的结果。"
        ),
        "metric": "contains",
        "expected_output": "公里",  # 浅锚：正确总结应提到单位；强判定靠 semantic 黑盒执行
        "steps": [
            # 强指标：distance.py 确实建了且含函数定义
            {
                "name": "impl",
                "check": "file_contains",
                "path": "distance.py",
                "needles": ["def haversine"],
                "weight": 2,
            },
            # 总结提到单位（弱）
            {
                "name": "mentions_unit",
                "check": "contains",
                "contains": ["公里"],
                "weight": 1,
            },
        ],
        "script": (
            "import sys, math, distance\n"
            "lat1,lon1,lat2,lon2 = map(float, sys.stdin.read().split())\n"
            'print(f"{distance.haversine(lat1,lon1,lat2,lon2):.3f}")\n'
        ),
        "cases": [
            # 北京→上海约 1067 公里；字符串切片匹配不到这种数值结论
            {
                "input": "39.9042 116.4074 31.2304 121.4737",
                "want": "1067",
                "kind": "number≈",
            },
            # 同一点 → 0
            {"input": "1 2 1 2", "want": "0.0", "kind": "number≈"},
            # 空输入 → 应报错（负向）：stdin 无内容会 split 失败
            {"input": "", "want": "", "kind": "lines", "expect_error": True},
        ],
        "negations": ["抱歉", "我不会"],
        "seed_files": {"__init__.py": ""},
    },
    # ── 8. 语义等价 + 边界：日程重叠（负向用例：空/非法输入应失败）────
    {
        "name": "schedule_overlap",
        "task": (
            "在临时目录新建 overlap.py，写一个函数 overlap(a0,a1,b0,b1) 判断两个半开区间 "
            "[a0,a1) 与 [b0,b1) 是否重叠，返回 True/False。程序从 stdin 读一行 "
            "`a0 a1 b0 b1`，输出 True 或 False。"
            "做完用一句话总结你得到的结果。"
        ),
        "metric": "contains",
        "expected_output": "重叠",  # 浅锚；强判定靠 semantic 黑盒执行（含空/边界负向）
        "script": (
            "import sys, overlap\n"
            "a0,a1,b0,b1 = map(int, sys.stdin.read().split())\n"
            "print(overlap.overlap(a0,a1,b0,b1))\n"
        ),
        "cases": [
            {"input": "1 5 3 7", "want": "True", "kind": "lines"},
            {"input": "1 2 3 4", "want": "False", "kind": "lines"},
            {"input": "2 2 2 2", "want": "False", "kind": "lines"},  # 空区间
            {"input": "1 5 5 9", "want": "False", "kind": "lines"},  # 半开相邻不重叠
            {"input": "", "want": "", "kind": "lines", "expect_error": True},  # 负向
        ],
        "negations": ["不知道", "报错但不解释"],
        "seed_files": {"__init__.py": ""},
    },
]
