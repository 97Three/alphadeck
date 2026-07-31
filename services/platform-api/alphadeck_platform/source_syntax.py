import ast
import re
from functools import lru_cache

import tree_sitter_css
import tree_sitter_typescript
from tree_sitter import Language, Parser


@lru_cache(maxsize=3)
def language(kind):
    return Language(
        tree_sitter_css.language()
        if kind == "css"
        else tree_sitter_typescript.language_tsx()
        if kind == "tsx"
        else tree_sitter_typescript.language_typescript()
    )


def parse_source(path, content):
    kind = (
        "css"
        if path.lower().endswith(".css")
        else "tsx"
        if path.lower().endswith(".tsx")
        else "typescript"
    )
    return Parser(language(kind)).parse(content.encode())


def statements(tree):
    return [
        node
        for node in tree.root_node.named_children
        if node.type not in {"comment", "hash_bang_line"}
    ]


def declaration(node):
    current = node
    while current.type in {"export_statement", "ambient_declaration"}:
        nested = current.child_by_field_name("declaration")
        if nested is None:
            nested = next(
                (
                    child
                    for child in current.named_children
                    if child.type not in {"comment", "decorator"}
                ),
                None,
            )
        if nested is None:
            break
        current = nested
    return current


def declaration_names(node):
    current = declaration(node)
    if current.type in {"lexical_declaration", "variable_declaration", "using_declaration"}:
        return [
            name.text.decode()
            for child in current.named_children
            if child.type == "variable_declarator"
            and (name := child.child_by_field_name("name")) is not None
            and name.type == "identifier"
        ]
    if current.type in {
        "function_declaration",
        "generator_function_declaration",
        "function_signature",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    }:
        name = current.child_by_field_name("name")
        return [name.text.decode()] if name is not None else []
    # The TS grammar exposes resource declarations as expression statements.
    match = re.match(rb"(?:await\s+)?using\s+([A-Za-z_$][\w$]*)\s*=", current.text)
    return [match[1].decode()] if match else []


def variable_count(node):
    return sum(child.type == "variable_declarator" for child in declaration(node).named_children)


def declaration_identity(node, modifier_values):
    current = declaration(node)
    kinds = {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "function_signature": "function",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type_alias",
        "enum_declaration": "enum",
        "lexical_declaration": "variable",
        "variable_declaration": "variable",
        "using_declaration": "variable",
    }
    kind = kinds.get(current.type)
    resource = re.match(rb"(?:await\s+)?using\s+", current.text)
    if resource:
        kind = "variable"
    if kind is None:
        return None
    name = current.child_by_field_name("name")
    if kind == "variable":
        first = next(
            (child for child in current.named_children if child.type == "variable_declarator"), None
        )
        name = first.child_by_field_name("name") if first is not None else None
    prefix = node.text[
        : max(0, name.start_byte - node.start_byte) if name is not None else len(node.text)
    ].decode()
    prefix = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", prefix)
    words = re.findall(r"[A-Za-z_]+", prefix)
    modifiers = sorted(
        modifier_values[word]
        for word in words
        if word in modifier_values and not (kind == "variable" and word == "const")
    )
    variable_kind = None
    if kind == "variable":
        source = current.text.decode()
        match = re.match(r"(await\s+using|using|const|let|var)\b", source)
        variable_kind = match[1].replace(" ", "_") if match else "var"
    return {
        "declarationKind": kind,
        "modifierKinds": modifiers,
        "functionAsync": "async" in words if kind == "function" else None,
        "functionGenerator": current.type == "generator_function_declaration" or "*" in prefix
        if kind == "function"
        else None,
        "variableKind": variable_kind,
    }


def default_export(tree):
    return any(
        node.type == "export_statement"
        and declaration_names(node)
        and any(child.type == "default" for child in node.children)
        for node in statements(tree)
    )


def client_directive(tree):
    for node in statements(tree):
        if (
            node.type == "expression_statement"
            and len(node.named_children) == 1
            and node.named_children[0].type == "string"
        ):
            try:
                if ast.literal_eval(node.named_children[0].text.decode()) == "use client":
                    return True
            except (ValueError, SyntaxError):
                pass
    return False


def css_rules(tree):
    result, pending = [], [tree.root_node]
    while pending:
        node = pending.pop()
        if node.type == "rule_set":
            selector = next(
                (child for child in node.named_children if child.type == "selectors"), None
            )
            if selector is not None:
                result.append((node, selector.text.decode().strip()))
        pending.extend(reversed(node.named_children))
    return result
