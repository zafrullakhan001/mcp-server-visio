"""Deterministic, COM-independent Mermaid flowchart planning and validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from pathlib import Path
from lark import Lark, Tree, UnexpectedInput


GRAMMAR = r"""
start: _SEP* ("flowchart" | "graph") DIRECTION _SEP+ statement*
?statement: subgraph | chain _SEP+
subgraph: "subgraph" ID ("[" label "]")? _SEP+ statement* "end" _SEP+
chain: node (ARROW edge_label? node)*
node: ID node_label?
?node_label: "[" label "]"       -> rectangle
           | "(" label ")"     -> rounded_rectangle
           | "((" label "))"   -> circle
           | "{" label "}"     -> diamond
           | "[(" label ")]"   -> cylinder
edge_label: "|" label "|"
?label: ESCAPED_STRING -> quoted
      | LABEL          -> unquoted
DIRECTION: "TB" | "TD" | "BT" | "LR" | "RL"
ARROW: "<-.->" | "<-->" | "-.->" | "-->" | "---" | "==>"
ID: /[A-Za-z_](?:[A-Za-z0-9_.\/]|-(?![-.>]))*/
LABEL: /[^\[\]{}()\n"|;]+/
_SEP: /[\r\n;]+/
%import common.ESCAPED_STRING
%import common.WS_INLINE
%ignore WS_INLINE
%ignore /%%(?!\{)[^\r\n]*/
"""

_PARSER = Lark(GRAMMAR, parser="lalr", propagate_positions=True)
BASIC_KINDS = {"rectangle", "rounded_rectangle", "circle", "diamond", "cylinder"}


class DiagramError(ValueError):
    """An actionable input, mapping, layout, or policy error."""


@dataclass
class Node:
    id: str
    label: str
    kind: str = "rectangle"
    parent: str | None = None
    line: int = 0


@dataclass
class Edge:
    id: str
    source: str
    target: str
    label: str = ""
    dashed: bool = False
    bidirectional: bool = False
    arrow: bool = True
    line: int = 0


@dataclass
class Group:
    id: str
    label: str
    parent: str | None = None


@dataclass
class Graph:
    direction: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)


def _label(tree: Tree) -> str:
    value = str(tree.children[0])
    text = json.loads(value) if tree.data == "quoted" else value.strip()
    for br in ("<br/>", "<br />", "<br>"):
        text = text.replace(br, "\n")
    if "<" in text or ">" in text:
        raise DiagramError("HTML labels other than <br> are not supported.")
    if not text.strip():
        raise DiagramError("Labels must not be empty.")
    return text


def parse_mermaid(source: str) -> Graph:
    """Parse the documented flowchart subset; never ignore unsupported syntax."""
    text = source.strip()
    lines = text.splitlines()
    if lines and lines[0] == "```mermaid" and lines[-1] == "```":
        text = "\n".join(lines[1:-1]).strip()
    try:
        tree = _PARSER.parse(text + "\n")
    except UnexpectedInput as exc:
        raise DiagramError(
            f"Unsupported or invalid Mermaid at line {exc.line}, column {exc.column}: "
            f"{exc.get_context(text).strip()}"
        ) from exc
    graph = Graph(str(tree.children[0]).replace("TD", "TB"))
    nodes: dict[str, Node] = {}
    groups: dict[str, Group] = {}
    explicit: set[str] = set()

    def add_node(item: Tree, parent: str | None) -> Node:
        node_id = str(item.children[0])
        spec = item.children[1] if len(item.children) > 1 else None
        label = _label(spec.children[0]) if isinstance(spec, Tree) else node_id
        kind = str(spec.data) if isinstance(spec, Tree) else "rectangle"
        if node_id in groups:
            raise DiagramError(f"ID '{node_id}' is used by a node and a subgraph.")
        if node_id in nodes:
            node = nodes[node_id]
            if spec is not None:
                if node_id in explicit and (node.label != label or node.kind != kind):
                    raise DiagramError(f"Conflicting declarations for '{node_id}' at line {item.meta.line}.")
                node.label, node.kind = label, kind
            if parent is not None:
                if node.parent is not None and node.parent != parent:
                    raise DiagramError(f"Node '{node_id}' belongs to multiple subgraphs.")
                node.parent = parent
        else:
            node = Node(node_id, label, kind, parent, item.meta.line)
            nodes[node_id] = node
        if spec is not None:
            explicit.add(node_id)
        return node

    def visit(item: Tree, parent: str | None = None) -> None:
        if item.data == "subgraph":
            group_id = str(item.children[0])
            if group_id in groups or group_id in nodes:
                raise DiagramError(f"Duplicate subgraph or node ID '{group_id}'.")
            children = item.children[1:]
            title = group_id
            if children and children[0].data in ("quoted", "unquoted"):
                title = _label(children[0])
                children = children[1:]
            groups[group_id] = Group(group_id, title, parent)
            for child in children:
                visit(child, group_id)
            return
        previous = add_node(item.children[0], parent)
        index = 1
        while index < len(item.children):
            arrow = str(item.children[index])
            index += 1
            label = ""
            if isinstance(item.children[index], Tree) and item.children[index].data == "edge_label":
                label = _label(item.children[index].children[0])
                index += 1
            following = add_node(item.children[index], parent)
            graph.edges.append(Edge(
                f"edge_{len(graph.edges) + 1}", previous.id, following.id, label,
                "." in arrow, arrow.startswith("<"), arrow != "---", item.meta.line,
            ))
            previous = following
            index += 1

    for statement in tree.children[1:]:
        visit(statement)
    graph.nodes = list(nodes.values())
    graph.groups = list(groups.values())
    if not graph.nodes:
        raise DiagramError("The flowchart must contain at least one node.")
    return graph


DEFAULT_PROFILE = Path(__file__).with_name("organisation_profile.json")


def load_profile(path: str = "") -> dict:
    """Load a complete, validated profile; reject misspelled policy keys."""
    with DEFAULT_PROFILE.open(encoding="utf-8") as stream:
        profile = json.load(stream)
    if path:
        with Path(path).open(encoding="utf-8") as stream:
            overrides = json.load(stream)
        if not isinstance(overrides, dict):
            raise DiagramError("Organisation profile must be a JSON object.")
        unknown = set(overrides) - set(profile)
        if unknown:
            raise DiagramError(f"Unknown organisation profile keys: {sorted(unknown)}")
        profile.update(overrides)
    if type(profile["schema_version"]) is not int or profile["schema_version"] != 1:
        raise DiagramError("Unsupported organisation profile schema_version.")
    for key in ("id", "version", "font", "container_master"):
        if not isinstance(profile[key], str) or not profile[key].strip():
            raise DiagramError(f"Profile '{key}' must be a nonempty string.")
    for key in ("template", "template_sha256"):
        if not isinstance(profile[key], str):
            raise DiagramError(f"Profile '{key}' must be a string.")
    fingerprint = profile["template_sha256"]
    if fingerprint and (len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint)):
        raise DiagramError("template_sha256 must be a lowercase SHA-256 fingerprint.")
    if profile["template"]:
        base = Path(path).resolve().parent if path else DEFAULT_PROFILE.parent
        profile["template"] = str((base / profile["template"]).resolve())
    positive = ("page_width", "page_height", "node_width", "node_height",
                "font_size", "edge_font_size", "minimum_font_size", "gap",
                "container_padding", "container_header", "line_weight")
    nonnegative = ("margin_left", "margin_right", "margin_top", "margin_bottom",
                   "header_reserved", "footer_reserved")
    for key in positive + nonnegative:
        value = profile[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise DiagramError(f"Profile '{key}' must be a finite number.")
        if value < 0 or (key in positive and value == 0):
            raise DiagramError(f"Invalid profile '{key}': {value}.")
    if min(profile["font_size"], profile["edge_font_size"]) < profile["minimum_font_size"]:
        raise DiagramError("Configured font sizes are below minimum_font_size.")
    for key in ("require_mapping", "preserve_template_size"):
        if not isinstance(profile[key], bool):
            raise DiagramError(f"Profile '{key}' must be boolean.")
    for key in ("aliases", "catalogue"):
        if not isinstance(profile[key], dict):
            raise DiagramError(f"Profile '{key}' must be an object.")
    for alias, target in profile["aliases"].items():
        if not isinstance(alias, str) or not isinstance(target, str):
            raise DiagramError("Alias names and targets must be strings.")
    for key, entry in profile["catalogue"].items():
        if not isinstance(entry, dict) or set(entry) != {"stencil", "master"}:
            raise DiagramError(f"Catalogue '{key}' requires exactly stencil and master.")
        if not all(isinstance(v, str) and v.strip() for v in entry.values()):
            raise DiagramError(f"Invalid stencil or master in catalogue '{key}'.")
    for key in ("approved_stencils", "required_metadata"):
        if not isinstance(profile[key], list) or not all(isinstance(v, str) for v in profile[key]):
            raise DiagramError(f"Profile '{key}' must be an array of strings.")
    for key in ("container_color", "connector_color"):
        value = profile[key]
        if not isinstance(value, str) or len(value) != 6 or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise DiagramError(f"Profile '{key}' must be a six-digit hex colour.")
    return profile


def resolve_mappings(graph: Graph, profile: dict, stencil_map: dict,
                     mappings: dict[str, str] | None = None) -> dict[str, dict]:
    explicit = mappings or {}
    unknown = set(explicit) - {node.id for node in graph.nodes}
    if unknown:
        raise DiagramError(f"Mappings reference unknown node IDs: {sorted(unknown)}")
    catalogue = {**stencil_map, **profile["catalogue"]}

    def normalise(value: str) -> str:
        return value.casefold().strip().replace("_", "-").replace(" ", "-")

    aliases: dict[str, set[str]] = {}
    for service, entry in catalogue.items():
        for name in (service, service.removeprefix("azure/"), entry["master"]):
            aliases.setdefault(normalise(name), set()).add(service)
    for alias, target in profile["aliases"].items():
        if target not in catalogue and not target.startswith("basic/"):
            raise DiagramError(f"Alias '{alias}' targets unknown service '{target}'.")
        aliases[normalise(alias)] = {target}
    resolved: dict[str, dict] = {}
    for node in graph.nodes:
        requested = explicit.get(node.id, node.label)
        if not isinstance(requested, str):
            raise DiagramError(f"Mapping for '{node.id}' must be a string.")
        candidates = {requested} if requested in catalogue or requested.startswith("basic/") else aliases.get(normalise(requested), set())
        if len(candidates) > 1:
            raise DiagramError(f"Ambiguous mapping for '{node.id}': {sorted(candidates)}. Supply an explicit mapping.")
        if not candidates:
            if node.id in explicit or profile["require_mapping"]:
                raise DiagramError(f"No approved mapping for '{node.id}' ({requested}).")
            candidates = {f"basic/{node.kind}"}
        service = next(iter(candidates))
        if service.startswith("basic/"):
            kind = service.removeprefix("basic/")
            if kind not in BASIC_KINDS:
                raise DiagramError(f"Unsupported basic master '{service}'.")
            resolved[node.id] = {"service": service, "kind": kind}
        else:
            entry = catalogue[service]
            approved = profile["approved_stencils"]
            if approved and entry["stencil"] not in approved:
                raise DiagramError(f"Stencil '{entry['stencil']}' is not approved for '{node.id}'.")
            resolved[node.id] = {"service": service, **entry}
    return resolved


def _text_size(text: str, font_size: float, width: float) -> tuple[float, float]:
    """Conservative planning estimate; actual text is measured again in Visio."""
    lines = text.splitlines() or [""]
    char_width = font_size / 72 * 0.65
    columns = max(1, int((width - 0.16) / char_width))
    rows = sum(max(1, math.ceil(len(line) / columns)) for line in lines)
    return width, rows * font_size / 72 * 1.4 + 0.16


def usable_bounds(profile: dict, page_width: float, page_height: float) -> dict:
    bounds = {
        "left": profile["margin_left"],
        "right": page_width - profile["margin_right"],
        "bottom": profile["margin_bottom"] + profile["footer_reserved"],
        "top": page_height - profile["margin_top"] - profile["header_reserved"],
    }
    if bounds["left"] >= bounds["right"] or bounds["bottom"] >= bounds["top"]:
        raise DiagramError("Template/profile margins leave no usable drawing area.")
    return bounds


def layout_graph(graph: Graph, profile: dict, resolved: dict[str, dict],
                 page_width: float, page_height: float) -> dict:
    """Layered compound layout. Cyclic peers share a rank; no readability shrinking."""
    nodes = {node.id: node for node in graph.nodes}
    groups = {group.id: group for group in graph.groups}
    boxes: dict[str, dict] = {}
    horizontal = graph.direction in ("LR", "RL")
    gap = profile["gap"]

    def children(parent: str | None) -> list[str]:
        return [g.id for g in graph.groups if g.parent == parent] + [
            n.id for n in graph.nodes if n.parent == parent
        ]

    def owner(node_id: str, parent: str | None) -> str | None:
        current = node_id
        current_parent = nodes[node_id].parent
        while current_parent != parent:
            if current_parent is None:
                return None
            current = current_parent
            current_parent = groups[current].parent
        return current

    def arrange(parent: str | None) -> tuple[float, float]:
        items = children(parent)
        if not items:
            raise DiagramError(f"Empty subgraph '{parent}' is not supported.")
        for item in items:
            if item in groups:
                arrange(item)
            else:
                width, text_height = _text_size(nodes[item].label, profile["font_size"], profile["node_width"])
                icon_height = profile["node_height"] if "stencil" in resolved[item] else 0
                height = max(profile["node_height"], text_height + icon_height)
                if resolved[item].get("kind") == "circle":
                    width = height = max(width, height)
                boxes[item] = {"x": 0.0, "y": 0.0, "width": width,
                               "height": height,
                               "icon_height": icon_height}
        adjacency = {item: set() for item in items}
        for edge in graph.edges:
            source, target = owner(edge.source, parent), owner(edge.target, parent)
            if source is not None and target is not None and source != target:
                adjacency[source].add(target)
        # Reachability collapses strongly connected peers without recursive DFS.
        reach: dict[str, set[str]] = {}
        for item in items:
            visited: set[str] = set()
            pending = list(adjacency[item])
            while pending:
                target = pending.pop()
                if target not in visited:
                    visited.add(target)
                    pending.extend(adjacency[target] - visited)
            reach[item] = visited
        components: list[list[str]] = []
        assigned: set[str] = set()
        for item in items:
            if item not in assigned:
                component = [other for other in items if other == item or (
                    other in reach[item] and item in reach[other]
                )]
                components.append(component)
                assigned.update(component)
        component_of = {item: index for index, component in enumerate(components) for item in component}
        ranks = [0] * len(components)
        for _ in components:
            changed = False
            for source in items:
                for target in adjacency[source]:
                    a, b = component_of[source], component_of[target]
                    if a != b and ranks[b] < ranks[a] + 1:
                        ranks[b] = ranks[a] + 1
                        changed = True
            if not changed:
                break
        layers: dict[int, list[str]] = {}
        for item in items:
            layers.setdefault(ranks[component_of[item]], []).append(item)
        label_gap = max(
            (_text_size(e.label, profile["edge_font_size"], profile["node_width"])[1]
             for e in graph.edges if e.label), default=0,
        )
        flow_gap = max(gap, label_gap + 0.16)
        cursor = 0.0
        cross_extent = 0.0
        for rank in sorted(layers):
            layer = layers[rank]
            flow_size = max(boxes[i]["width" if horizontal else "height"] for i in layer)
            cross_size = sum(boxes[i]["height" if horizontal else "width"] for i in layer) + gap * (len(layer) - 1)
            cross_extent = max(cross_extent, cross_size)
            cross = 0.0
            for item in layer:
                box = boxes[item]
                box["x" if horizontal else "y"] = cursor
                box["y" if horizontal else "x"] = cross
                cross += box["height" if horizontal else "width"] + gap
            cursor += flow_size + flow_gap
        flow_extent = cursor - flow_gap
        width, height = (flow_extent, cross_extent) if horizontal else (cross_extent, flow_extent)
        # Centre each rank to keep redundant branches balanced.
        for layer in layers.values():
            extent = sum(boxes[i]["height" if horizontal else "width"] for i in layer) + gap * (len(layer) - 1)
            for item in layer:
                boxes[item]["y" if horizontal else "x"] += (cross_extent - extent) / 2
        padding = profile["container_padding"] if parent else 0
        header = profile["container_header"] if parent else 0
        if parent:
            _, title_height = _text_size(groups[parent].label, profile["font_size"], max(width, profile["node_width"]))
            header = max(header, title_height)
        for item in items:
            boxes[item]["x"] += padding
            boxes[item]["y"] += padding + (0 if graph.direction == "BT" else header)
        width += padding * 2
        height += padding * 2 + header
        if parent:
            boxes[parent] = {"x": 0.0, "y": 0.0, "width": width, "height": height}
        return width, height

    width, height = arrange(None)
    bounds = usable_bounds(profile, page_width, page_height)
    if width > bounds["right"] - bounds["left"] or height > bounds["top"] - bounds["bottom"]:
        raise DiagramError(
            f"Layout needs {width:.2f} x {height:.2f} in but usable page is "
            f"{bounds['right'] - bounds['left']:.2f} x {bounds['top'] - bounds['bottom']:.2f} in. "
            "Use a larger approved template/page, reduce spacing within policy, or split the source."
        )
    origin_x = (bounds["left"] + bounds["right"] - width) / 2
    origin_y = (bounds["bottom"] + bounds["top"] - height) / 2
    absolute: dict[str, dict] = {}

    def place(parent: str | None, offset_x: float, offset_y: float) -> None:
        for item in children(parent):
            box = boxes[item]
            x, y = offset_x + box["x"], offset_y + box["y"]
            absolute[item] = {**box, "x": x, "y": y}
            if item in groups:
                place(item, x, y)

    place(None, 0.0, 0.0)
    for box in absolute.values():
        x, y = box["x"], box["y"]
        if graph.direction == "RL":
            x = width - x - box["width"]
        if graph.direction != "BT":
            y = height - y - box["height"]
        box["x"] = origin_x + x + box["width"] / 2
        box["y"] = origin_y + y + box["height"] / 2
    return {"boxes": absolute, "usable_bounds": bounds,
            "page_width": page_width, "page_height": page_height}


def build_plan(source: str, profile: dict, stencil_map: dict,
               mappings: dict[str, str] | None = None, metadata: dict[str, str] | None = None,
               template: dict | None = None) -> dict:
    graph = parse_mermaid(source)
    metadata = metadata or {}
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
        raise DiagramError("Metadata keys and values must be strings.")
    missing = [key for key in profile["required_metadata"] if not metadata.get(key, "").strip()]
    if missing:
        raise DiagramError(f"Missing required metadata: {missing}")
    resolved = resolve_mappings(graph, profile, stencil_map, mappings)
    preserve = template and profile["preserve_template_size"]
    width = template["page_width"] if preserve else profile["page_width"]
    height = template["page_height"] if preserve else profile["page_height"]
    geometry = layout_graph(graph, profile, resolved, width, height)
    plan = {
        "schema_version": 1, "source": source, "graph": asdict(graph),
        "profile": profile, "mappings": resolved, "metadata": metadata,
        "template": template, "layout": geometry,
    }
    plan["validation"] = validate_plan(plan)
    seal_plan(plan)
    return plan


def seal_plan(plan: dict) -> None:
    content = {key: value for key, value in plan.items() if key != "plan_id"}
    plan["plan_id"] = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def _bounds(box: dict) -> tuple[float, float, float, float]:
    return (box["x"] - box["width"] / 2, box["y"] - box["height"] / 2,
            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def validate_plan(plan: dict) -> dict:
    graph, layout = plan["graph"], plan["layout"]
    boxes = layout["boxes"]
    issues: list[dict] = []
    limits = layout["usable_bounds"]
    parents = {n["id"]: n["parent"] for n in graph["nodes"] + graph["groups"]}
    for item, box in boxes.items():
        left, bottom, right, top = _bounds(box)
        if left < limits["left"] - 0.001 or bottom < limits["bottom"] - 0.001 or right > limits["right"] + 0.001 or top > limits["top"] + 0.001:
            issues.append({"code": "OUTSIDE_DRAWING_AREA", "id": item, "severity": "error"})
        parent = parents[item]
        if parent:
            pl, pb, pr, pt = _bounds(boxes[parent])
            if left < pl or bottom < pb or right > pr or top > pt:
                issues.append({"code": "OUTSIDE_CONTAINER", "id": item, "severity": "error"})
    items = list(boxes)
    for index, a in enumerate(items):
        for b in items[index + 1:]:
            # Only compare siblings: parent-child overlap is intentional.
            if parents[a] != parents[b]:
                continue
            al, ab, ar, at = _bounds(boxes[a])
            bl, bb, br, bt = _bounds(boxes[b])
            if min(ar, br) - max(al, bl) > 0.001 and min(at, bt) - max(ab, bb) > 0.001:
                issues.append({"code": "SHAPE_OVERLAP", "ids": [a, b], "severity": "error"})
    return {"status": "failed" if issues else "passed", "issues": issues,
            "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
            "groups": len(graph["groups"])}
