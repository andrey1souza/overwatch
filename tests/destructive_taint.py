"""How the scan of asserts after a discarding step follows a reader's answer: through names, fields, the objects of a dataclass, and the parameters of the functions that are called with them.

Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

from destructive_readers import FALLIBLE_MODULE_READERS, called_name, fallible_readers, is_module_call

MUTATING_METHODS = frozenset({"append", "extend", "add", "update", "insert", "setdefault"})
PASSES_OVER_ASSIGNMENTS = 3
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
Function = ast.FunctionDef | ast.AsyncFunctionDef
FunctionIndex = dict[str, list[tuple[str, Function]]]
LiteralLists = dict[str, list[str]]
ClassFields = dict[str, list[str]]
ObjectFields = dict[str, frozenset[str]]
SELF = "self"


@dataclass
class Readings:
    """What a function holds of readers' answers: the names that hold one, the fields that a call sets from one, the objects of a dataclass that hold one in some fields, with those fields, and the readers, which include the names that readers were given."""
    names: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)
    readers: set[str] = field(default_factory=lambda: set(fallible_readers()))
    objects: ObjectFields = field(default_factory=dict)
    classes: ClassFields = field(default_factory=dict)


def is_fallible_read(node: ast.AST, readers: set[str]) -> bool:
    """Whether node calls a reader that raises or answers nothing when git, the file system or a process fails, by its name or by a name it was given."""
    return isinstance(node, ast.Call) and (is_module_call(node, FALLIBLE_MODULE_READERS) or called_name(node) in readers)


def is_construction(value: ast.expr) -> bool:
    """Whether value makes an object of a class, whose fields a reading may set without the object being the reading."""
    return isinstance(value, ast.Call) and called_name(value)[:1].isupper()


def tainted_by(value: ast.expr, readings: Readings) -> bool:
    """Whether value holds a reader's answer: it calls a fallible reader, or reads a name or a field that holds one."""
    return any(is_fallible_read(node, readings.readers) or (isinstance(node, ast.Name) and node.id in readings.names) or (isinstance(node, ast.Attribute) and node.attr in readings.fields)
               or is_tainted_field(node, readings) for node in ast.walk(value))


def is_tainted_field(node: ast.AST, readings: Readings) -> bool:
    """Whether node reads a field of an object of a dataclass that a reader's answer was given to."""
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr in readings.objects.get(node.value.id, frozenset())


def constructed_fields(value: ast.expr, readings: Readings) -> frozenset[str]:
    """The fields of the dataclass that value makes which it sets from a reader's answer, by position or by keyword; none for a class that is no dataclass of the source."""
    if not (isinstance(value, ast.Call) and called_name(value) in readings.classes):
        return frozenset()
    names = readings.classes[called_name(value)]
    positional = {names[index] for index, argument in enumerate(value.args) if index < len(names) and tainted_by(positional_value(argument), readings)}
    keyword = {item.arg for item in value.keywords if item.arg in names and tainted_by(item.value, readings)}
    return frozenset(positional | keyword)


def dataclass_fields(tree: ast.AST) -> ClassFields:
    """The fields of each dataclass of tree, in order."""
    found: ClassFields = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(called_name(item) == "dataclass" if isinstance(item, ast.Call) else (isinstance(item, ast.Name) and item.id == "dataclass") or (isinstance(item, ast.Attribute) and item.attr == "dataclass") for item in node.decorator_list):
            found[node.name] = [item.target.id for item in node.body if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)]
    return found


def keyword_fields(node: ast.AST, readings: Readings) -> set[str]:
    """The fields that a call sets from a reader's answer, as the head of replace(state, head=head)."""
    if not isinstance(node, ast.Call):
        return set()
    return {keyword.arg for keyword in node.keywords if keyword.arg and tainted_by(keyword.value, readings)}


def positional_value(value: ast.expr) -> ast.expr:
    """value without the keyword arguments of its calls: the answer a call gives holds its positional arguments, and its keyword fields only mark the fields."""
    class WithoutKeywords(ast.NodeTransformer):
        def visit_Call(self, node: ast.Call) -> ast.Call:
            node.keywords = []
            return self.generic_visit(node)

    return WithoutKeywords().visit(ast.parse(ast.unparse(value), mode="eval").body)


def bound_names(target: ast.AST) -> set[str]:
    """The names a target binds."""
    return {item.id for item in ast.walk(target) if isinstance(item, ast.Name)}


def bindings(node: ast.AST) -> list[tuple[set[str], ast.expr]]:
    """What node binds, as (the names, the value they take): an assignment, an annotated or augmented one, a walrus, the target of a for loop or comprehension, and the name of a with."""
    if isinstance(node, ast.Assign):
        return [(set().union(*(bound_names(target) for target in node.targets)), node.value)]
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
        return [(bound_names(node.target), node.value)]
    if isinstance(node, ast.NamedExpr):
        return [(bound_names(node.target), node.value)]
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return [(bound_names(node.target), node.iter)]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [(bound_names(item.optional_vars), item.context_expr) for item in node.items if item.optional_vars is not None]
    return []


def is_alias_of_reader(node: ast.AST, readers: set[str]) -> bool:
    """Whether node assigns a reader itself, not its answer, to a name."""
    value = node.value if isinstance(node, ast.Assign) else None
    return (isinstance(value, ast.Name) and value.id in readers) or (isinstance(value, ast.Attribute) and value.attr in readers)


def take_bindings(node: ast.AST, readings: Readings) -> None:
    """Records what node binds: an object of a dataclass with the fields it sets from a reader's answer, and a name that holds one."""
    for bound, value in bindings(node):
        fields = constructed_fields(value, readings)
        for name in bound if fields else ():
            readings.objects[name] = readings.objects.get(name, frozenset()) | fields
        if not is_construction(value) and tainted_by(positional_value(value), readings):
            readings.names |= bound


def takes_by_mutation(node: ast.AST, readings: Readings) -> bool:
    """Whether node adds a reader's answer to a name, as a call of append or update."""
    function = node.func if isinstance(node, ast.Call) else None
    return isinstance(function, ast.Attribute) and function.attr in MUTATING_METHODS and isinstance(function.value, ast.Name) and any(tainted_by(argument, readings) for argument in node.args)


def readings_of(function: Function, given: frozenset[str] = frozenset(), objects: ObjectFields | None = None, classes: ClassFields | None = None) -> Readings:
    """What function holds of readers' answers, with the parameters named in given holding one, and those named in objects holding an object of a dataclass with those fields set from one."""
    readings = Readings(names=set(given), objects=dict(objects or {}), classes=classes or {})
    for _ in range(PASSES_OVER_ASSIGNMENTS):
        for node in ast.walk(function):
            readings.fields |= keyword_fields(node, readings)
            if is_alias_of_reader(node, readings.readers):
                readings.readers |= bindings(node)[0][0]
            take_bindings(node, readings)
            if takes_by_mutation(node, readings):
                readings.names.add(node.func.value.id)
    assert all(readings.names), f"the names {readings.names} that hold readings include an empty one"
    return readings


def given_parameters(call: ast.Call, callee: Function, readings: Readings) -> frozenset[str]:
    """The parameters of callee that call gives a reader's answer: an argument that makes an object of a class is not one, as an assigned object is not."""
    parameters = [argument.arg for argument in (*callee.args.posonlyargs, *callee.args.args, *callee.args.kwonlyargs) if argument.arg != "self"]
    positional = {parameters[index] for index, argument in enumerate(call.args) if index < len(parameters) and not is_construction(argument) and tainted_by(positional_value(argument), readings)}
    keyword = {item.arg for item in call.keywords if item.arg in parameters and not is_construction(item.value) and tainted_by(item.value, readings)}
    return frozenset(positional | keyword)


def given_objects(call: ast.Call, callee: Function, readings: Readings) -> ObjectFields:
    """The parameters of callee that call gives an object of a dataclass that holds a reader's answer, with its fields, self included when the call is a method of such an object."""
    parameters = [argument.arg for argument in (*callee.args.posonlyargs, *callee.args.args, *callee.args.kwonlyargs) if argument.arg != SELF]
    given = {parameters[index]: readings.objects[argument.id] for index, argument in enumerate(call.args) if index < len(parameters) and isinstance(argument, ast.Name) and argument.id in readings.objects}
    given.update({item.arg: readings.objects[item.value.id] for item in call.keywords if item.arg in parameters and isinstance(item.value, ast.Name) and item.value.id in readings.objects})
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    if isinstance(receiver, ast.Name) and receiver.id in readings.objects:
        given[SELF] = readings.objects[receiver.id]
    return given
