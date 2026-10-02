"""命令行入口：python -m mev_shield [--input IN] [--output OUT]

缺省 --input / --output 时使用标准输入 / 标准输出。
任何失败均退出 2，并输出对应错误码。
"""

import sys

from . import core

_BAD_ARGS = "BAD_ARGS"
_INPUT_IO = "INPUT_IO"
_OUTPUT_IO = "OUTPUT_IO"

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_args(argv):
    """解析参数；任何不合法用法返回 None。

    仅接受 --input IN / --output OUT（含 --input=IN 形式），各至多一次，
    不接受位置参数或其他选项。
    """
    opts = {"input": None, "output": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        key = None
        value = None
        if arg in ("--input", "--output"):
            key = arg[2:]
            if i + 1 >= len(argv):
                return None
            value = argv[i + 1]
            i += 2
        elif arg.startswith("--input=") or arg.startswith("--output="):
            key, value = arg[2:].split("=", 1)
            i += 1
        else:
            return None
        if opts[key] is not None:
            return None
        opts[key] = value
    return opts


def _stderr(code, stderr):
    stderr.write(code + "\n")
    stderr.flush()


def run(argv=None, stdin_buffer=None, stdout_buffer=None, stderr=None):
    """执行一次处理，返回退出码。缓冲区参数用于测试注入。"""
    if argv is None:
        argv = sys.argv[1:]
    if stdin_buffer is None:
        stdin_buffer = sys.stdin.buffer
    if stdout_buffer is None:
        stdout_buffer = sys.stdout.buffer
    if stderr is None:
        stderr = sys.stderr

    opts = parse_args(argv)
    if opts is None:
        # 参数不可信，错误 JSON 写标准输出；stdout 也不可写时仅留 stderr。
        payload = core.serialize(core.error_result("", _BAD_ARGS)).encode("utf-8")
        try:
            stdout_buffer.write(payload)
            stdout_buffer.flush()
        except OSError:
            pass
        _stderr(_BAD_ARGS, stderr)
        return _EXIT_ERROR

    # 读取输入
    try:
        if opts["input"] is None:
            raw = stdin_buffer.read()
        else:
            with open(opts["input"], "rb") as f:
                raw = f.read()
    except OSError:
        result, prior_code = core.error_result("", _INPUT_IO), _INPUT_IO
    else:
        result, prior_code = core.process(raw)

    payload = core.serialize(result).encode("utf-8")
    exit_code = _EXIT_ERROR if prior_code is not None else _EXIT_OK

    # 写出结果
    try:
        if opts["output"] is None:
            stdout_buffer.write(payload)
            stdout_buffer.flush()
        else:
            with open(opts["output"], "wb") as f:
                f.write(payload)
    except OSError:
        # 输出不可写：仅向 stderr 写 code（已有更高优先级错误码时保留之）
        _stderr(prior_code if prior_code is not None else _OUTPUT_IO, stderr)
        return _EXIT_ERROR

    # 输出成功写出后，校验/输入类失败仍需向 stderr 写 code
    if prior_code is not None:
        _stderr(prior_code, stderr)
    return exit_code
