"""Incentive formulas (design/09-incentives.md section 4).

One expression per field, written by an admin, so it is never handed to
Python's eval. It is parsed with `ast` and walked over a whitelist:

  numbers ("1.5", and "1%" meaning 0.01), text in quotes (to compare `role`),
  variables, + - * /, brackets, < <= > >= == !=, and / or / not,
  `a if condition else b`, and the functions min max round abs.

Anything else -- attributes, indexing, other names or calls, **, lambdas,
comprehensions -- is refused when the scheme is saved (`check`). Arithmetic is
Decimal throughout; True and False count as 1 and 0.
"""

import ast
import re
from decimal import ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation

MAX_LENGTH = 500
FUNCTIONS = ("min", "max", "round", "abs")
# "1%" / "0.5 %" -> "(1/100)"; a % that is not after a number is left for the
# parser to refuse (Python's modulo is not offered).
PERCENT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*%")
NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class FormulaError(ValueError):
    """A formula that can't be used, with a message for the person."""


def _prepare(text):
    text = (text or "").strip()
    if not text:
        raise FormulaError("Enter a formula.")
    if len(text) > MAX_LENGTH:
        raise FormulaError(f"Keep a formula under {MAX_LENGTH} characters.")
    return PERCENT.sub(r"(\1/100)", text)


_BINOPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
_CMPOPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)


def parse(text):
    """The checked tree of `text`, or FormulaError."""
    source = _prepare(text)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError:
        raise FormulaError("The formula is not complete or has a typo (check brackets and operators).") from None
    _walk(tree.body)
    return tree.body


def _walk(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float | str):
            raise FormulaError("Only numbers and quoted text can be written as values.")
        return
    if isinstance(node, ast.Name):
        return
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BINOPS:
            raise FormulaError("Only + - * / can be used between values.")
        _walk(node.left)
        _walk(node.right)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub | ast.UAdd | ast.Not):
            raise FormulaError("That operator can't be used.")
        _walk(node.operand)
        return
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _walk(value)
        return
    if isinstance(node, ast.Compare):
        if not all(isinstance(op, _CMPOPS) for op in node.ops):
            raise FormulaError("Compare with < <= > >= == != only.")
        _walk(node.left)
        for value in node.comparators:
            _walk(value)
        return
    if isinstance(node, ast.IfExp):
        _walk(node.test)
        _walk(node.body)
        _walk(node.orelse)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            raise FormulaError(f"Only these functions can be used: {', '.join(FUNCTIONS)}.")
        if node.keywords or any(isinstance(a, ast.Starred) for a in node.args) or not node.args:
            raise FormulaError(f"Give {node.func.id}() its values, separated by commas.")
        if node.func.id == "abs" and len(node.args) != 1:
            raise FormulaError("abs() takes one value.")
        if node.func.id == "round" and len(node.args) > 2:
            raise FormulaError("round() takes a value and, optionally, the number of decimals.")
        for arg in node.args:
            _walk(arg)
        return
    raise FormulaError("That can't be used in a formula.")


def names_in(tree):
    """The variable names a checked tree uses."""
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - set(FUNCTIONS)


def check(text, known):
    """FormulaError unless `text` is a valid formula using only `known` names.
    Returns the parsed tree."""
    tree = parse(text)
    unknown = sorted(names_in(tree) - set(known))
    if unknown:
        raise FormulaError(f"Unknown variable{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}.")
    return tree


# -- Evaluating --------------------------------------------------------------------------

def _num(value):
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise FormulaError("Text can only be compared, not calculated with.")


def _truthy(value):
    return bool(value) if not isinstance(value, Decimal) else value != 0


def _eval(node, values):
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else _num(node.value)
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise FormulaError(f"Unknown variable: {node.id}.")
        value = values[node.id]
        return value if isinstance(value, str) else _num(value)
    if isinstance(node, ast.BinOp):
        left, right = _num(_eval(node.left, values)), _num(_eval(node.right, values))
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise FormulaError("Divides by zero.")
        try:
            return left / right
        except (DivisionByZero, InvalidOperation):
            raise FormulaError("Divides by zero.") from None
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, values)
        if isinstance(node.op, ast.Not):
            return Decimal(0 if _truthy(operand) else 1)
        operand = _num(operand)
        return -operand if isinstance(node.op, ast.USub) else operand
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result = Decimal(1)
            for value in node.values:
                result = _eval(value, values)
                if not _truthy(result):
                    return Decimal(0)
            return result
        for value in node.values:
            result = _eval(value, values)
            if _truthy(result):
                return result
        return Decimal(0)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, values)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, values)
            if isinstance(left, str) != isinstance(right, str) and not isinstance(op, ast.Eq | ast.NotEq):
                raise FormulaError("Text can only be compared with == or !=.")
            ok = {ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b,
                  ast.GtE: lambda a, b: a >= b, ast.Eq: lambda a, b: a == b,
                  ast.NotEq: lambda a, b: a != b}[type(op)](left, right)
            if not ok:
                return Decimal(0)
            left = right
        return Decimal(1)
    if isinstance(node, ast.IfExp):
        return _eval(node.body if _truthy(_eval(node.test, values)) else node.orelse, values)
    if isinstance(node, ast.Call):
        args = [_num(_eval(a, values)) for a in node.args]
        name = node.func.id
        if name == "min":
            return min(args)
        if name == "max":
            return max(args)
        if name == "abs":
            return abs(args[0])
        places = int(args[1]) if len(args) > 1 else 0
        return args[0].quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    raise FormulaError("That can't be used in a formula.")


def evaluate(tree_or_text, values):
    """The formula's value for these variable values: a Decimal (a comparison
    gives 1 or 0). Raises FormulaError, e.g. "Divides by zero."."""
    tree = parse(tree_or_text) if isinstance(tree_or_text, str) else tree_or_text
    result = _eval(tree, values)
    if isinstance(result, str):
        raise FormulaError("A formula must give a number.")
    return result


def is_name(text):
    """A derived variable's name: lower case letters, digits and _."""
    return bool(NAME.match(text or ""))
