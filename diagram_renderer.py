"""Template-aware Visio rendering. All entry points run on the COM worker."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from diagram_pipeline import (
    DiagramError, build_plan, load_profile, parse_mermaid, resolve_mappings,
    seal_plan, usable_bounds, validate_plan,
)

if TYPE_CHECKING:
    from visio_client import VisioClient


def _string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _user(shape, name: str, value: str) -> None:
    if not shape.CellExistsU(f"User.{name}", 0):
        shape.AddNamedRow(242, name, 0)
    shape.CellsU(f"User.{name}").FormulaU = _string(value)


def _read_user(shape, name: str) -> str:
    if not shape.CellExistsU(f"User.{name}", 0):
        raise DiagramError(f"Missing persisted diagram data: User.{name}.")
    return shape.CellsU(f"User.{name}").ResultStrU(0)


def _rgb(value: str) -> str:
    return f"RGB({int(value[0:2], 16)},{int(value[2:4], 16)},{int(value[4:6], 16)})"


def _geometry(shape) -> dict:
    return {key: shape.CellsU(cell).ResultIU for key, cell in (
        ("x", "PinX"), ("y", "PinY"), ("width", "Width"), ("height", "Height"),
    )}


def _fit(shape, box: dict) -> None:
    for key, cell in (("width", "Width"), ("height", "Height"), ("x", "PinX"), ("y", "PinY")):
        shape.CellsU(cell).ResultIU = box[key]


class DiagramWorkflow:
    def __init__(self, client: VisioClient):
        self.client = client
        self.plans: dict[str, dict] = {}

    def _target(self, document_id: int, page_id: int):
        app = self.client.app
        doc = next((app.Documents.Item(i) for i in range(1, app.Documents.Count + 1)
                    if app.Documents.Item(i).ID == document_id), None)
        if doc is None:
            raise DiagramError(f"Document ID {document_id} is not open.")
        page = next((doc.Pages.Item(i) for i in range(1, doc.Pages.Count + 1)
                     if doc.Pages.Item(i).ID == page_id), None)
        if page is None:
            raise DiagramError(f"Page ID {page_id} does not exist in document {document_id}.")
        return doc, page

    @staticmethod
    def _template_info(doc, page) -> dict:
        def fields(shapes) -> list[str]:
            result: list[str] = []
            for shape in shapes:
                if shape.NameU.startswith("MCP."):
                    result.append(shape.NameU[4:])
                if shape.Shapes.Count:
                    result.extend(fields(shape.Shapes))
            return result

        return {
            "page_width": page.PageSheet.CellsU("PageWidth").ResultIU,
            "page_height": page.PageSheet.CellsU("PageHeight").ResultIU,
            "drawing_scale": page.PageSheet.CellsU("DrawingScale").ResultIU,
            "page_scale": page.PageSheet.CellsU("PageScale").ResultIU,
            "pages": [{"id": p.ID, "name": p.Name, "background": bool(p.Background)}
                      for p in doc.Pages],
            "layers": [layer.Name for layer in page.Layers],
            "metadata_fields": sorted({key for p in doc.Pages for key in fields(p.Shapes)}),
        }

    def inspect_template(self, template: str) -> dict:
        path = Path(template).resolve(strict=True)
        if path.suffix.lower() not in (".vstx", ".vst", ".vsdx"):
            raise DiagramError("Template must be a .vstx, .vst, or .vsdx file.")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        doc = self.client.app.Documents.OpenEx(str(path), 1 | 2 | 8 | 64 | 128)
        try:
            page = next((p for p in doc.Pages if not p.Background), None)
            if page is None:
                raise DiagramError("Template has no foreground drawing page.")
            return {**self._template_info(doc, page), "path": str(path), "sha256": digest}
        finally:
            doc.Close()

    def resolve_stencil_mapping(self, source: str, profile_path: str = "",
                                mappings: dict[str, str] | None = None) -> dict:
        graph = parse_mermaid(source)
        profile = load_profile(profile_path)
        resolved = resolve_mappings(graph, profile, self.client._stencil_map, mappings)
        self._masters(resolved, profile, bool(graph.groups))
        return {"status": "passed", "mappings": resolved}

    def _masters(self, mappings: dict[str, dict], profile: dict, containers: bool) -> dict:
        masters = {}
        for node_id, entry in mappings.items():
            if "stencil" in entry:
                stencil_name = entry["stencil"]
                stencil = (self.client._open_azure_stencil(stencil_name)
                           if stencil_name.startswith("Azure-") and not Path(stencil_name).suffix
                           else self.client._open_stencil(stencil_name))
                masters[node_id] = stencil.Masters.ItemU(entry["master"])
            elif entry["kind"] == "cylinder":
                stencil = self.client._open_stencil("BASFLO_M.vssx")
                masters[node_id] = stencil.Masters.ItemU("Database")
        if containers:
            filename = self.client.app.GetBuiltInStencilFile(2, 0)
            stencil = self.client._open_stencil(filename)
            masters["__container__"] = stencil.Masters.ItemU(profile["container_master"])
        return masters

    def plan_mermaid_diagram(self, source: str, profile_path: str = "", template: str = "",
                             mappings: dict[str, str] | None = None,
                             metadata: dict[str, str] | None = None) -> dict:
        profile = load_profile(profile_path)
        approved_template = profile["template"]
        if template and approved_template and Path(template).resolve() != Path(approved_template):
            raise DiagramError("The supplied template does not match the organisation profile.")
        selected_template = approved_template or template
        info = self.inspect_template(selected_template) if selected_template else None
        if profile["template_sha256"] and (not info or info["sha256"] != profile["template_sha256"]):
            raise DiagramError("Template fingerprint does not match the organisation profile.")
        if info and abs(info["drawing_scale"] - info["page_scale"]) > 0.000001:
            raise DiagramError("Scaled drawing templates are not supported; use a 1:1 architecture template.")
        plan = build_plan(source, profile, self.client._stencil_map, mappings, metadata, info)
        self._masters(plan["mappings"], plan["profile"], bool(plan["graph"]["groups"]))
        if plan["validation"]["status"] != "passed":
            raise DiagramError(f"Plan validation failed: {plan['validation']['issues']}")
        # Bounded cache: callers can re-plan deterministically after eviction/restart.
        if len(self.plans) >= 32:
            self.plans.pop(next(iter(self.plans)))
        self.plans[plan["plan_id"]] = deepcopy(plan)
        return deepcopy(plan)

    @staticmethod
    def _write_manifest(page, manifest: dict) -> None:
        payload = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"))
        chunks = [payload[i:i + 1000] for i in range(0, len(payload), 1000)]
        for index, chunk in enumerate(chunks):
            _user(page.PageSheet, f"McpDiagram_{index}", chunk)
        _user(page.PageSheet, "McpDiagramCount", str(len(chunks)))

    @staticmethod
    def _manifest(page) -> dict:
        count = int(_read_user(page.PageSheet, "McpDiagramCount"))
        if count < 1 or count > 10000:
            raise DiagramError("Invalid persisted diagram chunk count.")
        return json.loads("".join(_read_user(page.PageSheet, f"McpDiagram_{i}") for i in range(count)))

    @staticmethod
    def _shape_map(page) -> dict[str, object]:
        result = {}
        for shape in page.Shapes:
            if shape.CellExistsU("User.McpSourceId", 0):
                key = _read_user(shape, "McpSourceId")
                if key in result:
                    raise DiagramError(f"Duplicate managed shape '{key}'. Remove the duplicate before updating.")
                result[key] = shape
        return result

    @staticmethod
    def _font(shape, doc, profile: dict, edge: bool = False) -> None:
        shape.CellsU("Char.Font").FormulaForceU = str(doc.Fonts.Item(profile["font"]).ID)
        shape.CellsU("Char.Size").FormulaForceU = f"{profile['edge_font_size'] if edge else profile['font_size']} pt"
        _user(shape, "McpTextHeight", "")
        shape.CellsU("User.McpTextHeight").FormulaU = "TEXTHEIGHT(TheText,TxtWidth)"

    def _draw(self, doc, page, plan: dict, masters: dict) -> dict:
        profile, graph = plan["profile"], plan["graph"]
        boxes, records = plan["layout"]["boxes"], {}
        shapes = {}

        def record(key: str, shape, layer_name: str) -> None:
            _user(shape, "McpSourceId", key)
            layer = next((item for item in page.Layers if item.Name == layer_name), None)
            if layer is None:
                layer = page.Layers.Add(layer_name)
            layer.Add(shape, 0)
            records[key] = shape.ID
            shapes[key] = shape

        for group in reversed(graph["groups"]):
            box = boxes[group["id"]]
            shape = page.Drop(masters["__container__"], box["x"], box["y"])
            shape.ContainerProperties.ResizeAsNeeded = 0
            shape.Text = group["label"]
            _fit(shape, box)
            # Native container themes guard these cells; the approved profile owns them.
            shape.CellsU("FillForegnd").FormulaForceU = _rgb(profile["container_color"])
            shape.CellsU("FillForegndTrans").FormulaForceU = "60%"
            shape.CellsU("LinePattern").FormulaForceU = "2"
            shape.CellsU("LineWeight").FormulaForceU = f"{profile['line_weight']} pt"
            shape.CellsU("LineColor").FormulaForceU = _rgb(profile["connector_color"])
            self._font(shape, doc, profile)
            shape.SendToBack()
            record(f"group:{group['id']}", shape, "MCP Boundaries")
        for node in graph["nodes"]:
            node_id, box = node["id"], boxes[node["id"]]
            mapping = plan["mappings"][node_id]
            if node_id in masters:
                shape = page.Drop(masters[node_id], box["x"], box["y"])
                if "stencil" in mapping:
                    # Preserve master artwork and aspect ratio; labels are separate shapes.
                    width = shape.CellsU("Width").ResultIU
                    height = shape.CellsU("Height").ResultIU
                    factor = min(box["width"] / width, box["icon_height"] / height)
                    shape.CellsU("Width").ResultIU = width * factor
                    shape.CellsU("Height").ResultIU = height * factor
                    shape.CellsU("PinY").ResultIU = box["y"] + (box["height"] - box["icon_height"]) / 2
                    shape.Text = ""
                    label_height = box["height"] - box["icon_height"]
                    label = page.DrawRectangle(
                        box["x"] - box["width"] / 2, box["y"] - box["height"] / 2,
                        box["x"] + box["width"] / 2, box["y"] - box["height"] / 2 + label_height,
                    )
                    label.Text = node["label"]
                    label.CellsU("LinePattern").FormulaU = "0"
                    label.CellsU("FillPattern").FormulaU = "0"
                    self._font(label, doc, profile)
                    record(f"label:{node_id}", label, "MCP Labels")
                else:
                    _fit(shape, box)
                    shape.Text = node["label"]
                    self._font(shape, doc, profile)
            else:
                l, b = box["x"] - box["width"] / 2, box["y"] - box["height"] / 2
                r, t = l + box["width"], b + box["height"]
                kind = mapping["kind"]
                if kind == "circle":
                    shape = page.DrawOval(l, b, r, t)
                elif kind == "diamond":
                    shape = page.DrawPolyline(
                        (box["x"], t, r, box["y"], box["x"], b, l, box["y"], box["x"], t), 0,
                    )
                else:
                    shape = page.DrawRectangle(l, b, r, t)
                    shape.CellsU("Rounding").FormulaU = "0.06 in" if kind == "rounded_rectangle" else "0 in"
                shape.Text = node["label"]
                shape.CellsU("FillForegnd").FormulaU = _rgb(profile["container_color"])
                shape.CellsU("FillForegndTrans").FormulaU = "15%"
                self._font(shape, doc, profile)
            _user(shape, "McpService", mapping["service"])
            record(f"node:{node_id}", shape, "MCP Components")
        for item in graph["groups"] + graph["nodes"]:
            if item["parent"]:
                parent = shapes[f"group:{item['parent']}"]
                key = f"group:{item['id']}" if f"group:{item['id']}" in shapes else f"node:{item['id']}"
                parent.ContainerProperties.AddMember(shapes[key], 2)
                if f"label:{item['id']}" in shapes:
                    parent.ContainerProperties.AddMember(shapes[f"label:{item['id']}"], 2)
        for edge in graph["edges"]:
            connector = page.Drop(self.client.app.ConnectorToolDataObject, 0, 0)
            connector.CellsU("BeginX").GlueTo(shapes[f"node:{edge['source']}"].CellsU("PinX"))
            connector.CellsU("EndX").GlueTo(shapes[f"node:{edge['target']}"].CellsU("PinX"))
            connector.CellsU("ShapeRouteStyle").FormulaU = "16"
            connector.CellsU("EndArrow").FormulaU = "4" if edge["arrow"] else "0"
            connector.CellsU("BeginArrow").FormulaU = "4" if edge["bidirectional"] else "0"
            connector.CellsU("EndArrowSize").FormulaU = "2"
            connector.CellsU("BeginArrowSize").FormulaU = "2"
            connector.CellsU("LineWeight").FormulaU = f"{profile['line_weight']} pt"
            connector.CellsU("LineColor").FormulaU = _rgb(profile["connector_color"])
            connector.CellsU("LinePattern").FormulaU = "2" if edge["dashed"] else "1"
            connector.CellsU("Rounding").FormulaU = "0.15 in"
            connector.Text = edge["label"]
            self._font(connector, doc, profile, edge=True)
            record(f"edge:{edge['id']}", connector, "MCP Connections")
        for template_page in doc.Pages:
            self._fill_metadata(template_page.Shapes, plan["metadata"])
        manifest = {"plan": plan, "shapes": records}
        self._write_manifest(page, manifest)
        return manifest

    def _fill_metadata(self, shapes, metadata: dict) -> None:
        for shape in shapes:
            if shape.NameU.startswith("MCP.") and shape.NameU[4:] in metadata:
                shape.Text = metadata[shape.NameU[4:]]
            if shape.Shapes.Count:
                self._fill_metadata(shape.Shapes, metadata)

    def render_diagram_plan(self, plan_id: str, file_path: str = "", preview_path: str = "") -> dict:
        if plan_id not in self.plans:
            raise DiagramError("Unknown or expired plan_id. Call plan_mermaid_diagram again.")
        plan = deepcopy(self.plans[plan_id])
        template = plan["template"]
        if template and hashlib.sha256(Path(template["path"]).read_bytes()).hexdigest() != template["sha256"]:
            raise DiagramError("Template changed after planning. Re-plan before rendering.")
        self._check_outputs(file_path, preview_path)
        masters = self._masters(plan["mappings"], plan["profile"], bool(plan["graph"]["groups"]))
        app = self.client.app
        doc = app.Documents.AddEx(template["path"] if template else "", 0, 128)
        page = next((p for p in doc.Pages if not p.Background), None)
        if page is None:
            doc.Saved = True
            doc.Close()
            raise DiagramError("Document has no foreground page.")
        scope = app.BeginUndoScope("Render Mermaid")
        try:
            # AutoSize can reset the page to printer dimensions during undo.
            page.AutoSize = False
            page.PageSheet.CellsU("PrintPageOrientation").FormulaU = (
                "2" if plan["layout"]["page_width"] >= plan["layout"]["page_height"] else "1"
            )
            for key, cell in (("page_width", "PageWidth"), ("page_height", "PageHeight")):
                page.PageSheet.CellsU(cell).ResultIU = plan["layout"][key]
            self._draw(doc, page, plan, masters)
            validation = self.validate_diagram(doc.ID, page.ID)
            if validation["status"] == "failed":
                raise DiagramError(f"Rendered diagram failed validation: {validation['issues']}")
        except Exception:
            app.EndUndoScope(scope, False)
            doc.Saved = True
            doc.Close()
            raise
        else:
            app.EndUndoScope(scope, True)
        return self._finish(doc, page, plan_id, validation, file_path, preview_path)

    @staticmethod
    def _check_outputs(file_path: str, preview_path: str) -> None:
        for value, suffixes in ((file_path, {".vsdx"}), (preview_path, {".png", ".svg", ".jpg"})):
            if value:
                path = Path(value).resolve()
                if path.suffix.lower() not in suffixes or not path.parent.is_dir():
                    raise DiagramError(f"Invalid output path or missing parent directory: {path}")
                if path.exists():
                    raise DiagramError(f"Output already exists; choose a new path: {path}")

    @staticmethod
    def _finish(doc, page, plan_id: str, validation: dict, file_path: str, preview_path: str) -> dict:
        result = {"status": validation["status"], "document_id": doc.ID, "page_id": page.ID,
                  "plan_id": plan_id, "validation": validation}
        # Export before SaveAs so export failure never leaves a supposedly complete saved diagram.
        if preview_path:
            page.Export(os.path.abspath(preview_path))
            result["preview_path"] = os.path.abspath(preview_path)
        if file_path:
            doc.SaveAs(os.path.abspath(file_path))
            result["file_path"] = os.path.abspath(file_path)
        return result

    def create_from_mermaid(self, source: str, profile_path: str = "", template: str = "",
                            mappings: dict[str, str] | None = None,
                            metadata: dict[str, str] | None = None,
                            file_path: str = "", preview_path: str = "") -> dict:
        plan = self.plan_mermaid_diagram(source, profile_path, template, mappings, metadata)
        return self.render_diagram_plan(plan["plan_id"], file_path, preview_path)

    def validate_diagram(self, document_id: int, page_id: int) -> dict:
        doc, page = self._target(document_id, page_id)
        manifest = self._manifest(page)
        plan = deepcopy(manifest["plan"])
        shapes = self._shape_map(page)
        issues: list[dict] = []
        expected = manifest["shapes"]
        for key in set(expected) - set(shapes):
            issues.append({"code": "MISSING_SHAPE", "id": key, "severity": "error"})
        for key in set(shapes) - set(expected):
            issues.append({"code": "UNEXPECTED_MANAGED_SHAPE", "id": key, "severity": "error"})
        profile = plan["profile"]
        for node in plan["graph"]["nodes"]:
            key, label_key = f"node:{node['id']}", f"label:{node['id']}"
            if key not in shapes:
                continue
            shape = shapes[key]
            box = _geometry(shape)
            if label_key in shapes:
                label_box = _geometry(shapes[label_key])
                left = min(box["x"] - box["width"] / 2, label_box["x"] - label_box["width"] / 2)
                right = max(box["x"] + box["width"] / 2, label_box["x"] + label_box["width"] / 2)
                bottom = min(box["y"] - box["height"] / 2, label_box["y"] - label_box["height"] / 2)
                top = max(box["y"] + box["height"] / 2, label_box["y"] + label_box["height"] / 2)
                box = {"x": (left + right) / 2, "y": (bottom + top) / 2,
                       "width": right - left, "height": top - bottom}
            plan["layout"]["boxes"][node["id"]] = box
            expected_mapping = plan["mappings"][node["id"]]
            if _read_user(shape, "McpService") != expected_mapping["service"]:
                issues.append({"code": "SERVICE_CHANGED", "id": key, "severity": "error"})
            if "stencil" in expected_mapping and (shape.Master is None or shape.Master.NameU != expected_mapping["master"]):
                issues.append({"code": "MASTER_CHANGED", "id": key, "severity": "error"})
        for group in plan["graph"]["groups"]:
            key = f"group:{group['id']}"
            if key in shapes:
                plan["layout"]["boxes"][group["id"]] = _geometry(shapes[key])
                members = set(shapes[key].ContainerProperties.GetMemberShapes(0) or ())
                for item in plan["graph"]["nodes"] + plan["graph"]["groups"]:
                    child_key = ("group:" if item in plan["graph"]["groups"] else "node:") + item["id"]
                    if item["parent"] == group["id"] and child_key in shapes and shapes[child_key].ID not in members:
                        issues.append({"code": "MISSING_CONTAINER_MEMBERSHIP", "id": child_key, "severity": "error"})
        expected_text = {f"label:{n['id']}" if "stencil" in plan["mappings"][n["id"]] else f"node:{n['id']}": n["label"]
                         for n in plan["graph"]["nodes"]}
        expected_text.update({f"group:{g['id']}": g["label"] for g in plan["graph"]["groups"]})
        expected_text.update({f"edge:{e['id']}": e["label"] for e in plan["graph"]["edges"]})
        for key, text in expected_text.items():
            if key not in shapes:
                continue
            shape = shapes[key]
            if shape.Text != text:
                issues.append({"code": "LABEL_CHANGED", "id": key, "severity": "error"})
            if text:
                edge = key.startswith("edge:")
                expected_size = profile["edge_font_size"] if edge else profile["font_size"]
                if abs(shape.CellsU("Char.Size").ResultIU * 72 - expected_size) > 0.01:
                    issues.append({"code": "FONT_SIZE_CHANGED", "id": key, "severity": "error"})
                if shape.CellsU("Char.Font").ResultIU != doc.Fonts.Item(profile["font"]).ID:
                    issues.append({"code": "FONT_CHANGED", "id": key, "severity": "error"})
                if not edge and not key.startswith("group:"):
                    if not shape.CellExistsU("User.McpTextHeight", 0):
                        issues.append({"code": "MISSING_TEXT_METRICS", "id": key, "severity": "error"})
                    elif shape.CellsU("User.McpTextHeight").ResultIU > shape.CellsU("TxtHeight").ResultIU + 0.02:
                        issues.append({"code": "TEXT_OVERFLOW", "id": key, "severity": "error"})
        def check_title_fields(items) -> None:
            for item in items:
                if item.NameU.startswith("MCP.") and item.NameU[4:] in plan["metadata"]:
                    if item.Text != plan["metadata"][item.NameU[4:]]:
                        issues.append({"code": "METADATA_FIELD_CHANGED", "id": item.NameU, "severity": "error"})
                if item.Shapes.Count:
                    check_title_fields(item.Shapes)

        for template_page in doc.Pages:
            check_title_fields(template_page.Shapes)
        for edge in plan["graph"]["edges"]:
            key = f"edge:{edge['id']}"
            if key not in shapes:
                continue
            connector = shapes[key]
            connected = {connection.FromCell.Name: connection.ToSheet.ID for connection in connector.Connects}
            for cell, endpoint in (("BeginX", edge["source"]), ("EndX", edge["target"])):
                if f"node:{endpoint}" in shapes and connected.get(cell) != shapes[f"node:{endpoint}"].ID:
                    issues.append({"code": "DISCONNECTED_EDGE", "id": key, "endpoint": cell, "severity": "error"})
            for cell, expected_value in (
                ("EndArrow", 4 if edge["arrow"] else 0),
                ("BeginArrow", 4 if edge["bidirectional"] else 0),
                ("LinePattern", 2 if edge["dashed"] else 1),
            ):
                if connector.CellsU(cell).ResultIU != expected_value:
                    issues.append({"code": "CONNECTOR_STYLE_CHANGED", "id": key, "cell": cell, "severity": "error"})
            if abs(connector.CellsU("LineWeight").ResultIU * 72 - profile["line_weight"]) > 0.01:
                issues.append({"code": "CONNECTOR_WEIGHT_CHANGED", "id": key, "severity": "error"})
            if connector.CellsU("LineColor").FormulaU != _rgb(profile["connector_color"]):
                issues.append({"code": "CONNECTOR_COLOR_CHANGED", "id": key, "severity": "error"})
        width = page.PageSheet.CellsU("PageWidth").ResultIU
        height = page.PageSheet.CellsU("PageHeight").ResultIU
        if abs(width - plan["layout"]["page_width"]) > 0.001 or abs(height - plan["layout"]["page_height"]) > 0.001:
            issues.append({"code": "PAGE_SIZE_CHANGED", "severity": "error"})
        plan["layout"]["usable_bounds"] = usable_bounds(profile, width, height)
        issues.extend(validate_plan(plan)["issues"])
        # Routing intersections need visual inspection; do not claim pixel-perfect certification.
        issues.append({"code": "VISUAL_REVIEW_REQUIRED", "severity": "warning",
                       "message": "Review preview for connector crossings, edge labels and template artwork collisions."})
        return {"status": "failed" if any(i["severity"] == "error" for i in issues) else "needs_review",
                "issues": issues, "nodes": len(plan["graph"]["nodes"]),
                "edges": len(plan["graph"]["edges"]), "groups": len(plan["graph"]["groups"])}

    def update_from_mermaid(self, document_id: int, page_id: int, source: str,
                            mappings: dict[str, str] | None = None,
                            metadata: dict[str, str] | None = None,
                            preserve_manual_positions: bool = True,
                            file_path: str = "", preview_path: str = "") -> dict:
        doc, page = self._target(document_id, page_id)
        manifest = self._manifest(page)
        old = manifest["plan"]
        overrides = mappings if mappings is not None else {
            node_id: entry["service"] for node_id, entry in old["mappings"].items()
            if node_id in {node.id for node in parse_mermaid(source).nodes}
        }
        info = dict(old["template"] or {})
        info.update(page_width=page.PageSheet.CellsU("PageWidth").ResultIU,
                    page_height=page.PageSheet.CellsU("PageHeight").ResultIU)
        plan = build_plan(source, old["profile"], self.client._stencil_map, overrides,
                          metadata if metadata is not None else old["metadata"], info)
        plan["template"] = old["template"]
        existing = self._shape_map(page)
        if preserve_manual_positions:
            for node_id, box in plan["layout"]["boxes"].items():
                key = f"group:{node_id}" if f"group:{node_id}" in existing else f"node:{node_id}"
                if key not in existing or node_id not in old["layout"]["boxes"]:
                    continue
                before = old["layout"]["boxes"][node_id]
                actual = _geometry(existing[key])
                if key.startswith("node:") and "stencil" in old["mappings"].get(node_id, {}):
                    actual["y"] -= (before["height"] - before["icon_height"]) / 2
                if abs(actual["x"] - before["x"]) > 0.01 or abs(actual["y"] - before["y"]) > 0.01:
                    box["x"], box["y"] = actual["x"], actual["y"]
        validation = validate_plan(plan)
        if validation["status"] != "passed":
            raise DiagramError(f"Preserved manual positions violate layout rules: {validation['issues']}. "
                               "Retry with preserve_manual_positions=false to re-layout.")
        plan["validation"] = validation
        seal_plan(plan)
        self._check_outputs(file_path, preview_path)
        masters = self._masters(plan["mappings"], plan["profile"], bool(plan["graph"]["groups"]))
        app = self.client.app
        scope = app.BeginUndoScope("Update Mermaid")
        try:
            # Only delete managed shapes, never template/user artwork.
            for shape in list(existing.values()):
                if not shape.CellExistsU("User.McpSourceId", 0):
                    raise DiagramError("Managed shape identity changed during update.")
                shape.Delete()
            self._draw(doc, page, plan, masters)
            result = self.validate_diagram(document_id, page_id)
            if result["status"] == "failed":
                raise DiagramError(f"Updated diagram failed validation: {result['issues']}")
        except Exception:
            app.EndUndoScope(scope, False)
            raise
        else:
            app.EndUndoScope(scope, True)
        return self._finish(doc, page, plan["plan_id"], result, file_path, preview_path)
