"""Bundled skill: precise arithmetic via a safe AST evaluator.

Self-contained — no core imports. A good first read for how a skill
is shaped: a handler(arg, chat_id) -> str, plus a SKILL dict.
"""
import ast
import math

_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round,
          "floor": math.floor, "ceil": math.ceil, "log": math.log,
          "log10": math.log10, "log2": math.log2, "sin": math.sin,
          "cos": math.cos, "tan": math.tan, "min": min, "max": max}
_NAMES = {"pi": math.pi, "e": math.e}
_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
        ast.Pow: lambda a, b: a ** b}


def _eval(node):
    """Whitelist AST walk — the safe alternative to eval(). Anything
    not explicitly allowed raises."""
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 256:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](a, b)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS and not node.keywords):
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    raise ValueError(f"unsupported: {type(node).__name__}")


def calc(arg: str, chat_id: int) -> str:
    expr = arg.strip().replace("^", "**")
    try:
        val = _eval(ast.parse(expr, mode="eval"))
    except (ValueError, TypeError, SyntaxError, ZeroDivisionError,
            OverflowError) as e:
        return f"(couldn't evaluate: {e})"
    if isinstance(val, float):
        val = int(val) if (val == int(val) and abs(val) < 1e15) \
            else round(val, 10)
    if isinstance(val, int) and len(str(val)) > 18:
        return f"{arg.strip()} ≈ {float(val):.6e}"
    return f"{arg.strip()} = {val}"


SKILL = {
    "name": "calc",
    "desc": "Do precise arithmetic — never compute multi-step math in "
            "your head. Supports + - * / ** % (), and sqrt, log, sin, "
            "cos, round, floor, ceil, min, max, abs, pi, e. "
            "Emit <calc>(17.5*12)/0.85</calc>",
    "handler": calc,
}
