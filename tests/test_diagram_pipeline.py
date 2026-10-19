from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from diagram_pipeline import (
    DiagramError, build_plan, load_profile, parse_mermaid, resolve_mappings, validate_plan,
)


class ParserTests(unittest.TestCase):
    def test_nodes_chains_labels_and_flags(self):
        graph = parse_mermaid('flowchart TD\nA["Web API"] -->|HTTPS| B[(Orders)] <-.-> C{Ready}')
        self.assertEqual(graph.direction, "TB")
        self.assertEqual([(n.id, n.label, n.kind) for n in graph.nodes], [
            ("A", "Web API", "rectangle"), ("B", "Orders", "cylinder"),
            ("C", "Ready", "diamond"),
        ])
        self.assertEqual(graph.edges[0].label, "HTTPS")
        self.assertTrue(graph.edges[1].dashed)
        self.assertTrue(graph.edges[1].bidirectional)

    def test_nested_groups_forward_references_and_hyphen_ids(self):
        graph = parse_mermaid(
            'graph LR\nclient-web --> api\nsubgraph cloud["Production"]\n'
            'subgraph vnet[Network]\napi(API)\nend\nend\napi --- db[(Orders)]'
        )
        self.assertEqual(graph.nodes[1].parent, "vnet")
        self.assertEqual(graph.groups[1].parent, "cloud")
        self.assertFalse(graph.edges[1].arrow)

    def test_comments_fences_and_line_breaks(self):
        graph = parse_mermaid('```mermaid\nflowchart BT\n%% explanation\nA["Line 1<br/>Line 2"]\n```')
        self.assertEqual(graph.nodes[0].label, "Line 1\nLine 2")
        self.assertEqual(graph.direction, "BT")
        self.assertEqual(parse_mermaid("```mermaid\r\nflowchart TD\r\nA --> B\r\n```").direction, "TB")

    def test_semicolons(self):
        self.assertEqual(len(parse_mermaid("graph TB; A-->B; B-->C").edges), 2)

    def test_rejects_unsupported_input_without_silent_loss(self):
        for source in (
            "sequenceDiagram\nAlice->>Bob: hi",
            "flowchart TD\nA-->B\nclassDef blue fill:blue",
            "flowchart TD\nA-->B\nstyle A fill:red",
            "flowchart TD\nA --> missing[",
            "flowchart TD\n%%{init: {}}%%\nA",
            "flowchart TD\nA --> B & C",
            "flowchart TD\nA[<b>Unsafe</b>]",
            "flowchart TD\nsubgraph group\nend",
        ):
            with self.subTest(source=source), self.assertRaises(DiagramError):
                parse_mermaid(source)
                profile = load_profile()
                profile["aliases"] = {}
                build_plan(source, profile, {})

    def test_rejects_conflicting_identity(self):
        for source in (
            "graph TB\nA[First]\nA[Second]",
            "graph TB\nsubgraph A\nA\nend",
            "graph TB\nsubgraph first\nA\nend\nsubgraph second\nA\nend",
        ):
            with self.subTest(source=source), self.assertRaises(DiagramError):
                parse_mermaid(source)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile()
        self.catalogue = {
            "azure/front-door": {"stencil": "Azure-Web", "master": "Front Door And Cdn Profiles"},
            "azure/cdn": {"stencil": "Azure-Web", "master": "Front Door And Cdn Profiles"},
            "azure/sql-database": {"stencil": "Azure-Databases", "master": "SQL Database"},
        }

    def test_explicit_mapping_and_approved_alias(self):
        graph = parse_mermaid("graph TB\nweb[Front Door] --> db[Orders]")
        result = resolve_mappings(graph, self.profile, self.catalogue, {"db": "azure/sql-database"})
        self.assertEqual(result["web"]["service"], "azure/front-door")
        self.assertEqual(result["db"]["master"], "SQL Database")

    def test_ambiguity_missing_mapping_and_approval(self):
        scenarios = [
            ("graph TB\nA[Front Door And Cdn Profiles]", {}, self.profile),
            ("graph TB\nA", {"A": "azure/not-a-service"}, self.profile),
            ("graph TB\nA", {"missing": "azure/sql-database"}, self.profile),
            ("graph TB\nA", {}, {**self.profile, "require_mapping": True}),
            ("graph TB\nA[SQL Database]", {}, {**self.profile, "approved_stencils": ["Azure-Web"]}),
        ]
        for source, mappings, profile in scenarios:
            with self.subTest(source=source, mappings=mappings), self.assertRaises(DiagramError):
                resolve_mappings(parse_mermaid(source), profile, self.catalogue, mappings)

    def test_no_heuristic_service_guess(self):
        result = resolve_mappings(parse_mermaid("graph TB\nDB[Orders database]"), self.profile, self.catalogue)
        self.assertEqual(result["DB"]["service"], "basic/rectangle")

    def test_custom_catalogue_and_explicit_basic(self):
        profile = {**self.profile, "catalogue": {"company/router": {"stencil": "Network.vssx", "master": "Router"}}}
        result = resolve_mappings(parse_mermaid("graph TB\nA --> B"), profile, self.catalogue,
                                  {"A": "company/router", "B": "basic/diamond"})
        self.assertEqual(result["A"]["stencil"], "Network.vssx")
        self.assertEqual(result["B"]["kind"], "diamond")

    def test_profile_rejects_invalid_rules(self):
        for overrides in ({"gpa": 1}, {"gap": -1}, {"gap": True}, {"gap": float("nan")},
                          {"font_size": 2}, {"approved_stencils": "Azure-Web"},
                          {"aliases": {"db": 1}}, {"require_mapping": "yes"},
                          {"catalogue": {"x": {"stencil": "bad"}}}):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "profile.json"
                path.write_text(json.dumps(overrides), encoding="utf-8")
                with self.assertRaises(DiagramError):
                    load_profile(str(path))

    def test_required_metadata_and_unknown_alias_target(self):
        with self.assertRaises(DiagramError):
            build_plan("graph TB\nA", {**self.profile, "required_metadata": ["owner"]}, self.catalogue)
        with self.assertRaises(DiagramError):
            resolve_mappings(parse_mermaid("graph TB\nA"),
                             {**self.profile, "aliases": {"bad": "missing"}}, self.catalogue)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile()
        self.profile["aliases"] = {}

    def plan(self, source, **kwargs):
        return build_plan(source, self.profile, {}, **kwargs)

    def test_all_directions_and_determinism(self):
        for direction in ("TB", "TD", "BT", "LR", "RL"):
            with self.subTest(direction=direction):
                source = f"flowchart {direction}\nA --> B\nA --> C"
                plan = self.plan(source)
                self.assertEqual(plan, self.plan(source))
                self.assertEqual(plan["validation"]["status"], "passed")
                a, b, c = (plan["layout"]["boxes"][k] for k in ("A", "B", "C"))
                if direction in ("TB", "TD"):
                    self.assertGreater(a["y"], b["y"])
                elif direction == "BT":
                    self.assertLess(a["y"], b["y"])
                elif direction == "LR":
                    self.assertLess(a["x"], b["x"])
                else:
                    self.assertGreater(a["x"], b["x"])
                self.assertEqual(b["y"] if direction in ("TB", "TD", "BT") else b["x"],
                                 c["y"] if direction in ("TB", "TD", "BT") else c["x"])

    def test_nested_groups_and_cycles(self):
        plan = self.plan("graph TB\nsubgraph cloud[Cloud]\nsubgraph net[Network]\nA --> B\nB --> A\nend\nend\nB --> C")
        self.assertEqual(plan["validation"]["status"], "passed")
        self.assertEqual(plan["validation"]["nodes"], 3)
        self.assertEqual(plan["validation"]["edges"], 3)

    def test_template_dimensions_and_reserved_space(self):
        self.profile.update(header_reserved=1.5, footer_reserved=0.75)
        plan = self.plan("graph LR\nA --> B", template={"page_width": 17, "page_height": 11})
        self.assertEqual(plan["layout"]["page_width"], 17)
        self.assertEqual(plan["layout"]["usable_bounds"]["top"], 8.5)
        self.assertEqual(plan["layout"]["usable_bounds"]["bottom"], 1.75)

    def test_too_large_fails_instead_of_shrinking(self):
        with self.assertRaisesRegex(DiagramError, "larger approved template"):
            self.plan("graph TB\n" + " --> ".join(f"N{i}" for i in range(30)))

    def test_text_estimate_expands_node_and_circle_is_round(self):
        plan = self.plan('graph LR\nA["A long label that requires wrapping across multiple lines"] --> B((User))')
        boxes = plan["layout"]["boxes"]
        self.assertGreater(boxes["A"]["height"], self.profile["node_height"])
        self.assertEqual(boxes["B"]["width"], boxes["B"]["height"])

    def test_validation_detects_geometry_errors(self):
        plan = self.plan("graph TB\nA --> B")
        altered = deepcopy(plan)
        altered["layout"]["boxes"]["B"] = dict(altered["layout"]["boxes"]["A"])
        self.assertIn("SHAPE_OVERLAP", {i["code"] for i in validate_plan(altered)["issues"]})
        altered["layout"]["boxes"]["B"]["x"] = 100
        self.assertIn("OUTSIDE_DRAWING_AREA", {i["code"] for i in validate_plan(altered)["issues"]})


if __name__ == "__main__":
    unittest.main()
