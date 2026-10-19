import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from server import mcp
from visio_client import VisioWorker


class MCPToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_schemas_and_backward_compatible_tools(self):
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        for name in ("inspect_template", "resolve_stencil_mapping", "plan_mermaid_diagram",
                     "render_diagram_plan", "create_from_mermaid", "validate_diagram",
                     "update_from_mermaid"):
            with self.subTest(name=name):
                self.assertIsNotNone(tools[name].outputSchema)
                self.assertEqual(tools[name].outputSchema["type"], "object")
        for name in ("create_diagram", "add_shape", "add_azure_shape", "connect_shapes", "export_page"):
            self.assertIn(name, tools)
        self.assertIn("document_id", tools["update_from_mermaid"].inputSchema["required"])
        self.assertIn("page_id", tools["validate_diagram"].inputSchema["required"])

    async def test_structured_result_not_just_json_text(self):
        with patch("server.visio.workflow", return_value={"status": "passed", "mappings": {}}):
            _, structured = await mcp.call_tool("resolve_stencil_mapping", {"source": "graph TB\nA"})
        self.assertEqual(structured, {"status": "passed", "mappings": {}})


class WorkerTests(unittest.TestCase):
    def test_legacy_and_workflow_calls_share_one_thread(self):
        class FakeClient:
            def __init__(self):
                self.owner = threading.get_ident()
                self.workflow = self

            def probe(self):
                return self.owner, threading.get_ident()

            def quit(self):
                self.probe()

        with patch("visio_client.VisioClient", FakeClient):
            worker = VisioWorker()
            try:
                with ThreadPoolExecutor(max_workers=4) as callers:
                    calls = [callers.submit(worker.probe) for _ in range(4)]
                    calls += [callers.submit(worker.workflow, "probe") for _ in range(4)]
                    values = [call.result() for call in calls]
                self.assertEqual(len(set(values)), 1)
                self.assertEqual(values[0][0], values[0][1])
                self.assertNotEqual(values[0][0], threading.get_ident())
            finally:
                worker.shutdown()


if __name__ == "__main__":
    unittest.main()
