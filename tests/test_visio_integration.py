"""Opt-in real COM checks; use an isolated Visio instance, never the user's session."""

import json
import os
from pathlib import Path
import tempfile
import unittest

import pythoncom
import win32com.client

from diagram_pipeline import DiagramError
from visio_client import VisioClient


@unittest.skipUnless(os.environ.get("VISIO_INTEGRATION") == "1", "Set VISIO_INTEGRATION=1 for real Visio")
class VisioIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pythoncom.CoInitialize()
        cls.app = win32com.client.DispatchEx("Visio.Application")
        cls.client = VisioClient()
        cls.client._app = cls.app

    @classmethod
    def tearDownClass(cls):
        for doc in list(cls.app.Documents):
            doc.Saved = True
            doc.Close()
        cls.app.Quit()
        cls.client._app = None
        pythoncom.CoUninitialize()

    def test_basic_shapes_save_reopen_update_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "diagram.vsdx")
            preview = str(Path(directory) / "diagram.svg")
            result = self.client.workflow.create_from_mermaid(
                "flowchart LR\nA[Client] -->|HTTPS| B(API)\nB <-.-> C[(Orders)]",
                file_path=output, preview_path=preview,
            )
            self.assertEqual(result["status"], "needs_review")
            self.assertGreater(Path(output).stat().st_size, 0)
            self.assertGreater(Path(preview).stat().st_size, 0)
            doc, page = self.client.workflow._target(result["document_id"], result["page_id"])
            unrelated = page.DrawRectangle(0, 0, 0.2, 0.2)
            unrelated.NameU = "UserArtwork"
            doc.Save()
            doc.Close()
            reopened = self.app.Documents.Open(output)
            page = reopened.Pages.Item(1)
            self.assertEqual(self.client.workflow.validate_diagram(reopened.ID, page.ID)["status"], "needs_review")
            self.assertTrue(reopened.Saved, "Validation must not dirty a saved drawing")
            updated = self.client.workflow.update_from_mermaid(
                reopened.ID, page.ID, "flowchart LR\nA[Client] --> B(API)\nB --> D[Worker]",
                preserve_manual_positions=False,
            )
            self.assertEqual(updated["validation"]["nodes"], 3)
            self.assertEqual(page.Shapes.ItemU("UserArtwork").NameU, "UserArtwork")
            again = self.client.workflow.update_from_mermaid(
                reopened.ID, page.ID, "flowchart LR\nA[Client] --> B(API)\nB --> D[Worker]",
                preserve_manual_positions=False,
            )
            self.assertEqual(again["validation"]["nodes"], 3)
            shapes = self.client.workflow._shape_map(page)
            shapes["edge:edge_1"].CellsU("EndArrow").FormulaU = "0"
            findings = self.client.workflow.validate_diagram(reopened.ID, page.ID)
            self.assertEqual(findings["status"], "failed")
            self.assertIn("CONNECTOR_STYLE_CHANGED", {i["code"] for i in findings["issues"]})
            reopened.Saved = True
            reopened.Close()

    def test_template_geometry_metadata_and_background_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            template = str(Path(directory) / "company.vstx")
            doc = self.app.Documents.Add("")
            page = doc.Pages.Item(1)
            page.PageSheet.CellsU("PageWidth").ResultIU = 17
            page.PageSheet.CellsU("PageHeight").ResultIU = 11
            background = doc.Pages.Add()
            background.Background = True
            background.Name = "Brand"
            logo = background.DrawRectangle(0, 9.8, 2, 10.8)
            logo.Text = "Company"
            title = page.DrawRectangle(3, 9.5, 10, 10.5)
            title.NameU = "MCP.owner"
            title.Text = "Owner"
            page.BackPage = background.Name
            doc.SaveAs(template)
            doc.Close()
            profile_path = Path(directory) / "profile.json"
            profile_path.write_text(json.dumps({"header_reserved": 1.5, "required_metadata": ["owner"]}))
            info = self.client.workflow.inspect_template(template)
            self.assertEqual(info["page_width"], 17)
            self.assertIn("owner", info["metadata_fields"])
            self.client.create_diagram(template)
            legacy = self.app.ActiveDocument
            self.assertEqual(legacy.Pages.Item(1).PageSheet.CellsU("PageWidth").ResultIU, 17)
            self.assertIn(legacy.ID, {entry["document_id"] for entry in self.client.list_open_diagrams()})
            legacy.Saved = True
            legacy.Close()
            result = self.client.workflow.create_from_mermaid(
                "flowchart LR\nA --> B", str(profile_path), template, metadata={"owner": "Architecture"},
            )
            created, drawing = self.client.workflow._target(result["document_id"], result["page_id"])
            self.assertEqual(drawing.PageSheet.CellsU("PageWidth").ResultIU, 17)
            self.assertEqual(drawing.Shapes.ItemU("MCP.owner").Text, "Architecture")
            self.assertEqual(created.Pages.Count, 2)
            self.assertTrue(drawing.BackPage)
            drawing.Shapes.ItemU("MCP.owner").Text = "Wrong owner"
            self.assertIn("METADATA_FIELD_CHANGED", {
                issue["code"] for issue in self.client.workflow.validate_diagram(created.ID, drawing.ID)["issues"]
            })
            created.Saved = True
            created.Close()

    def test_real_containers_and_official_icons(self):
        result = self.client.workflow.create_from_mermaid(
            "flowchart LR\nsubgraph net[Production network]\nweb[API] --> db[Orders]\nend",
            mappings={"web": "azure/app-services", "db": "azure/sql-database"},
        )
        doc, page = self.client.workflow._target(result["document_id"], result["page_id"])
        shapes = self.client.workflow._shape_map(page)
        self.assertEqual(shapes["node:web"].Master.NameU, "App Services")
        self.assertEqual(shapes["node:db"].Master.NameU, "SQL Database")
        members = set(shapes["group:net"].ContainerProperties.GetMemberShapes(0))
        self.assertTrue({shapes["node:web"].ID, shapes["node:db"].ID}.issubset(members))
        updated = self.client.workflow.update_from_mermaid(
            doc.ID, page.ID,
            "flowchart LR\nsubgraph net[Production network]\nweb[API] --> db[Orders]\nend",
            preserve_manual_positions=False,
        )
        self.assertEqual(updated["status"], "needs_review")
        doc.Saved = True
        doc.Close()

    def test_failed_validation_rolls_back_new_document(self):
        from unittest.mock import patch
        drawings_before = {doc.ID for doc in self.app.Documents if doc.Type == 1}
        plan = self.client.workflow.plan_mermaid_diagram("graph TB\nA --> B")
        with patch.object(self.client.workflow, "validate_diagram",
                          return_value={"status": "failed", "issues": [{"code": "TEST"}]}):
            with self.assertRaisesRegex(DiagramError, "failed validation"):
                self.client.workflow.render_diagram_plan(plan["plan_id"])
        self.assertEqual(drawings_before, {doc.ID for doc in self.app.Documents if doc.Type == 1})

    def test_nested_containers_and_manual_positions(self):
        source = "flowchart BT\nsubgraph cloud[Cloud]\nsubgraph net[Network]\nA[First] --> B[Second]\nend\nend"
        result = self.client.workflow.create_from_mermaid(source)
        doc, page = self.client.workflow._target(result["document_id"], result["page_id"])
        shapes = self.client.workflow._shape_map(page)
        before_x = shapes["node:A"].CellsU("PinX").ResultIU
        shapes["node:A"].CellsU("PinX").ResultIU = before_x + 0.1
        updated = self.client.workflow.update_from_mermaid(doc.ID, page.ID, source)
        self.assertEqual(updated["status"], "needs_review")
        self.assertAlmostEqual(self.client.workflow._shape_map(page)["node:A"].CellsU("PinX").ResultIU,
                               before_x + 0.1)
        # The active document is deliberately unrelated; explicit targeting must still work.
        other = self.app.Documents.Add("")
        try:
            self.assertEqual(self.client.workflow.validate_diagram(doc.ID, page.ID)["status"], "needs_review")
        finally:
            other.Saved = True
            other.Close()
        doc.Saved = True
        doc.Close()

    def test_update_rollback_restores_shapes_and_source(self):
        from unittest.mock import patch
        result = self.client.workflow.create_from_mermaid("graph LR\nA --> B")
        doc, page = self.client.workflow._target(result["document_id"], result["page_id"])
        before = self.client.workflow._manifest(page)
        before_ids = {shape.ID for shape in page.Shapes}
        with patch.object(self.client.workflow, "validate_diagram",
                          return_value={"status": "failed", "issues": [{"code": "TEST"}]}):
            with self.assertRaisesRegex(DiagramError, "failed validation"):
                self.client.workflow.update_from_mermaid(doc.ID, page.ID, "graph LR\nA --> C",
                                                        preserve_manual_positions=False)
        self.assertEqual(self.client.workflow._manifest(page), before)
        self.assertEqual({shape.ID for shape in page.Shapes}, before_ids)
        validation = self.client.workflow.validate_diagram(doc.ID, page.ID)
        self.assertEqual(validation["status"], "needs_review", validation)
        doc.Saved = True
        doc.Close()

    def test_template_policy_and_stale_plan_fail_before_drawing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.vstx"
            doc = self.app.Documents.Add("")
            doc.SaveAs(str(path))
            doc.Close()
            profile_path = Path(directory) / "profile.json"
            profile_path.write_text(json.dumps({"template": path.name}))
            plan = self.client.workflow.plan_mermaid_diagram("graph TB\nA", str(profile_path))
            self.assertEqual(plan["template"]["path"], str(path))
            with self.assertRaisesRegex(DiagramError, "does not match"):
                self.client.workflow.plan_mermaid_diagram("graph TB\nA", str(profile_path),
                                                         str(Path(directory) / "other.vstx"))
            doc = self.app.Documents.Open(str(path))
            doc.Pages.Item(1).DrawRectangle(0, 0, 1, 1)
            doc.Save()
            doc.Close()
            before = {doc.ID for doc in self.app.Documents}
            with self.assertRaisesRegex(DiagramError, "Template changed"):
                self.client.workflow.render_diagram_plan(plan["plan_id"])
            self.assertEqual({doc.ID for doc in self.app.Documents}, before)


if __name__ == "__main__":
    unittest.main()
