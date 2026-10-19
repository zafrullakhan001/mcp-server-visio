# Visio MCP Server

Are you a solution architect looking to build high‑quality architecture diagrams or evolve existing ones to reflect recent changes? We’ve got you covered.

The Visio MCP (Model Context Protocol) Server exposes Microsoft Visio diagram operations as tools, enabling you to generate production‑grade architecture diagrams from text descriptions, images, and documents.

By directly controlling Microsoft Visio, this MCP server supports not only initial diagram generation but also iterative refinement and evolution. You can continuously improve and adapt diagrams until they meet the highest quality standards.

The Visio MCP Server is designed for use with GitHub Copilot CLI and VS Code Agent Mode, but it can also be integrated with other LLM-based systems.

![diagram](image.png)

## Demo

<a href="./assets/screen-demo.gif">
  <img src="./assets/demo.gif" alt="Demo" width="600">
</a>

## Features

- **Azure service icons** — 206 Azure services from official Visio stencils (auto-discovered)
- **Architecture helpers** — tier bands, containers, connectors with style-guide compliance
- **Shape operations** — add, modify, remove, connect, and list shapes
- **Multi-page support** — add pages, switch between them
- **Export** — PNG, SVG, JPG output
- **Mermaid compiler** — deterministic flowchart planning, stencil preflight, and native Visio rendering
- **Organisation profiles** — approved templates/stencils, typography, margins, metadata, and readable-size constraints
- **Validation and updates** — persisted Mermaid source, targeted page updates, and structured findings

## Prerequisites

- **Microsoft Visio Professional** (installed and licensed)
- **Python 3.11+**
- **Azure Visio stencils** — download from [Microsoft Azure Architecture Icons](https://learn.microsoft.com/en-us/azure/architecture/icons/) and extract to your `My Shapes` folder

## Installation

```bash
cd visio-mcp
pip install -r requirements.txt
```

## Configuration

Add to your Copilot CLI MCP config (`~/.copilot/mcp-config.json`):

```json
{
  "mcpServers": {
    "visio": {
      "type": "stdio",
      "command": "python",
      "args": ["C:\\path\\to\\visio-mcp\\server.py"],
      "env": {}
    }
  }
}
```

### Whitelisting All Visio Tools

By default, Copilot CLI prompts for approval on each MCP tool call. To auto-approve all Visio MCP tools while keeping other tools at normal approval prompts, launch with:

```bash
copilot --allow-tool "visio"
```

To whitelist a specific tool only:

```bash
copilot --allow-tool "visio(add_azure_shape)"
```

To persist across sessions, add to `~/.copilot/config.json`:

```json
{
  "allowedTools": ["visio"]
}
```

## Available Tools

| Tool | Description |
|---|---|
| `create_diagram` | Create a new Visio diagram (landscape, 11×8.5 in) |
| `save_diagram` | Save to `.vsdx` file |
| `close_diagram` | Close without saving |
| `list_open_diagrams` | List all open documents |
| `add_shape` | Add basic shapes (rectangle, ellipse, diamond, etc.) |
| `add_azure_shape` | Add Azure service icons from official stencils |
| `remove_shape` | Remove a shape by ID |
| `modify_shape` | Change text, position, size, or color |
| `list_shapes` | List all shapes on the active page |
| `connect_shapes` | Connect two shapes with styled connectors |
| `remove_connection` | Remove a connector |
| `add_container` | Add a grouping boundary rectangle |
| `add_tier_band` | Add a full-width horizontal tier band |
| `add_text_label` | Add a floating text label |
| `list_azure_services` | List all 206 available Azure service keys |
| `list_stencil_masters` | List masters in a specific stencil |
| `open_stencil` | Open an Azure stencil by name |
| `add_page` | Add a new page |
| `set_active_page` | Switch to a page by index |
| `list_pages` | List all pages |
| `export_page` | Export page as image (PNG, SVG, JPG) |
| `inspect_template` | Inspect template dimensions, scale, backgrounds, layers, and title fields |
| `resolve_stencil_mapping` | Resolve Mermaid nodes and verify required masters are installed |
| `plan_mermaid_diagram` | Parse, apply policy, calculate layout, and cache a renderable plan |
| `render_diagram_plan` | Render a cached plan into a new native Visio document |
| `create_from_mermaid` | Plan, render, validate, optionally save and export in one call |
| `validate_diagram` | Validate a managed page using explicit document/page IDs |
| `update_from_mermaid` | Rebuild managed objects without deleting template/user artwork |

## Mermaid to organisation-standard Visio

The compiler preserves supported nodes, links, labels, and nested groups. It uses
deterministic compound graph layout, not AI-generated coordinates. Components are
native editable shapes; groups use real Visio container membership. Official
service icons retain their master artwork and aspect ratio, with separate labels.

Example arguments for `create_from_mermaid`:

```json
{
  "source": "flowchart LR\nsubgraph production[Production network]\nweb[Orders API] -->|SQL/TLS| db[Orders database]\nend",
  "mappings": {
    "web": "azure/app-services",
    "db": "azure/sql-database"
  },
  "metadata": {
    "owner": "Architecture team"
  },
  "file_path": "C:\\diagrams\\orders.vsdx",
  "preview_path": "C:\\diagrams\\orders.svg"
}
```

Output directories must already exist and output files must not exist. Empty
output paths leave the document open without saving/exporting. Use absolute
paths for templates, profiles, drawings, and previews.

For approval before drawing, call `plan_mermaid_diagram`, examine its graph,
mappings, geometry, and validation, then pass its `plan_id` to
`render_diagram_plan`. Planning can open stencils and a temporary read-only
template copy, but creates no drawing. Plans are cached in memory (32 entries)
and must be regenerated after server restart or eviction.

### Organisation profiles

The checked-in [organisation_profile.json](organisation_profile.json) is a
starting profile, **not certification against your organisation's actual rules**.
Pass `profile_path` to load a JSON object overriding its top-level keys.
Unknown keys, invalid values, ambiguous aliases, and missing required metadata
are errors. Nested objects such as `aliases` are replaced, not recursively merged.

Example company profile:

```json
{
  "id": "company-architecture",
  "version": "2026.1",
  "template": "Company Architecture.vstx",
  "header_reserved": 0.75,
  "footer_reserved": 0.25,
  "font": "Arial",
  "font_size": 10,
  "edge_font_size": 8,
  "minimum_font_size": 8,
  "approved_stencils": ["Azure-App Services", "Azure-Databases"],
  "require_mapping": true,
  "required_metadata": ["owner", "classification"],
  "aliases": {
    "SQL": "azure/sql-database",
    "API hosting": "azure/app-services"
  }
}
```

- `template` is relative to the profile file or absolute. When set, a caller
  cannot substitute another template. Optional `template_sha256` pins its contents.
  Every planned template is also fingerprinted to detect changes before rendering.
- `preserve_template_size` defaults to true. Blank pages use the profile's
  `page_width`/`page_height` (inches). Only 1:1 drawing-scale templates are supported.
- Margins plus `header_reserved`/`footer_reserved` define the usable drawing area.
  Reserve space explicitly for logos and title blocks; their positions are not
  automatically inferred from artwork.
- `node_width`, `node_height`, `gap`, `container_padding`, and
  `container_header` are in inches; font sizes and `line_weight` are in points.
  Layout never shrinks fonts to squeeze oversized diagrams onto a page.
- `catalogue` adds or overrides canonical services, for example:
  `"company/router": {"stencil": "C:\\stencils\\Company.vssx", "master": "Router"}`.
  Master lookup uses universal names (`NameU`). `container_master` defaults to
  the built-in `Plain` container master.
- An empty `approved_stencils` list allows all mapped stencils. A nonempty list
  restricts logical stencil names/paths exactly as recorded in the catalogue.
  Native basic shapes and the built-in container stencil are separate from this
  service-stencil allowlist.
- Explicit node mappings win over aliases. Unknown explicit mappings and missing
  masters fail; there is no generic fallback for them. With `require_mapping`
  false, ordinary unmapped nodes use their Mermaid basic shape. Set it to true
  to require an explicit mapping or approved alias for every node.
- Basic mappings are `basic/rectangle`, `basic/rounded_rectangle`,
  `basic/circle`, `basic/diamond`, and `basic/cylinder`.
- To populate template title fields, name their shapes `MCP.owner`,
  `MCP.classification`, etc., using the universal shape name. Matching metadata
  fills these shapes, including grouped shapes and background pages. Metadata
  is also stored in the drawing.
- Profile policy takes precedence over presentation in the input. Mermaid
  styling directives are rejected rather than allowed to bypass the profile.

### Supported Mermaid subset

- `flowchart` / `graph`: `TB`, `TD`, `BT`, `LR`, `RL`.
- Node IDs beginning with a letter/underscore; subsequent letters, digits,
  underscores, dots, slashes, and non-arrow hyphens.
- Nodes: `A[Rectangle]`, `A(Rounded)`, `A((Circle))`, `A{Decision}`,
  `A[(Database)]`; quoted labels and `<br>` line breaks.
- Named, nested subgraphs: `subgraph network[Production network] ... end`.
- Chained links: `A --> B --> C`.
- Links: `-->`, `-.->`, `<-->`, `<-.->`, `---`, `==>`.
  `==>` preserves connectivity but uses the organisation's standard line weight.
- Edge labels in pipes: `A -->|HTTPS| B`.
- Newline/semicolon statement separators, ordinary `%%` comments, and optional
  fenced Mermaid input.

Unsupported syntax raises an error with a source location. This includes
sequence/state/class diagrams, styles/classes, init directives, click actions,
inline edge labels (`-- text -->`), fan-out shorthand (`A --> B & C`),
subgraph-local directions, and edges whose endpoint is a subgraph itself.
Use explicit nodes and edges instead. Empty subgraphs and conflicting declarations
are rejected.

### Validation, persistence, and update behaviour

New workflow tools return structured MCP objects. `needs_review` means automated
checks passed, **not** a pixel-perfect certification. Findings include codes and
severities. Checks cover missing/extra managed objects, labels, masters, container
membership, fonts, text height, connector glue/arrows/styles, page bounds, and
unintended component overlaps.

**Always inspect the exported preview.** Connector crossings, edge-label placement,
collisions with template artwork, and icon-internal appearance still require visual
review. The layout is a deterministic layered compound implementation, not a
global crossing optimiser. Automatic repair and architecture/security semantics
are not inferred. Oversized layouts fail with guidance to use a larger approved
page/template or split the source; automatic pagination is not implemented.

Rendering/updating runs inside an undo scope. Failed rendering rolls back and closes
only its newly created document; failed updates roll back the existing page.
Managed drawing pages disable Visio AutoSize so automatic printer/page resizing
cannot invalidate the planned geometry during edits or undo.
Save/export errors are surfaced to the caller; these filesystem operations are
not an atomic multi-file transaction.

The source, resolved mappings, profile, layout, metadata, and managed-shape
identifiers are embedded in page ShapeSheet user cells and survive `.vsdx`
save/reopen. No external source sidecar is required. Use `list_open_diagrams`
(`document_id`) and `list_pages` (`page_id`) to obtain current IDs after reopening.

`update_from_mermaid` requires explicit document/page IDs. It rebuilds only
managed objects, so numeric Visio shape IDs can change. Unmanaged artwork stays
intact. Existing mappings/profile are reused unless replacement mappings are
supplied; supplying a mappings object replaces, rather than merges, the old
mapping overrides. Manually moved component/container positions are retained when
valid; set `preserve_manual_positions=false` to discard them and re-layout.
Other manual changes to managed formatting, labels, connectors, and sizes are
replaced. Official icon labels are separate objects and are not grouped with icons.

Both legacy tools and new workflows execute on a single COM worker thread.
The new workflows do not depend on the user's currently active drawing/page.
Legacy tools retain their active-page behaviour.

### Tests

Run the parser, policy, layout, tool-schema, and worker tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Opt into real Visio integration checks (requires Visio and the mapped Azure masters):

```powershell
$env:VISIO_INTEGRATION = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Integration tests create and close their own separate Visio instance and use
temporary output directories. They do not attach to or close your running Visio.

## Azure Stencil Discovery

The server automatically discovers Azure stencils by querying Visio's COM properties:

1. `Visio.Application.MyShapesPath` — user stencils folder
2. `Visio.Application.GetBuiltInStencilFile()` — built-in content directory
3. `Visio.Application.Path` — install location
4. Windows Shell `CSIDL_PERSONAL` — real Documents folder (handles OneDrive redirect)

Discovered stencil directories are registered in Visio's `StencilPaths` for native COM resolution. No hardcoded paths.
Both downloaded `Azure-*.vssx` and built-in `AZURE..._M.VSSX` /
`AZURE..._U.VSSX` naming conventions are supported. The service catalogue is a
mapping; workflow preflight verifies the actual installed masters before drawing.

## Style Guide

Legacy drawing helpers apply the defaults in `STYLE_GUIDE.md`:

- Shapes: rounded corners (0.06 in), 15% transparent fills
- Connectors: filled triangle arrowheads, 1 pt weight, 7 pt label font
- Containers: dashed border, 60% transparent, 9 pt label
- Tier bands: 70% transparent, bold 8 pt label

The Mermaid workflow uses the machine-readable organisation profile instead.
Official icon artwork is preserved rather than indiscriminately recoloured,
rounded, or made transparent. See the profile section above.

## Example

```
Create a 3-tier Azure architecture with Front Door, VM Scale Sets in 2 availability zones, and Azure SQL with replication
```

The server will create a professional Visio diagram with proper Azure icons, tier bands, containers, and styled connectors.

## See also
Also you can explore other MCP for simpiar tasks that generates architecture in different formats:
| Project | Type | Description | Link |
|------|------|------------|------|
| **draw.io MCP (official)** | MCP Server | Official draw.io Model Context Protocol server. Opens and generates diagrams directly in the draw.io editor. Supports native XML, CSV, and Mermaid inputs. | https://github.com/jgraph/drawio-mcp |
| **drawio-mcp-server** | MCP Server | Community MCP server providing full programmatic control over draw.io diagrams, including creation, inspection, modification, and iterative refinement via an embedded editor. | https://github.com/lgazo/drawio-mcp-server |
| **draw_architecture_mcp** | MCP Server | Architecture‑focused MCP server for generating and maintaining draw.io diagrams from structured models (e.g. YAML). Well suited for large infrastructure and network diagrams. | https://github.com/dwgeneral/draw_architecture_mcp |
| **Excalidraw MCP** | MCP Server | MCP server for generating and evolving hand‑drawn‑style diagrams using Excalidraw, often paired with draw.io MCPs for different visual styles. | https://github.com/excalidraw/excalidraw-mcp |
| **Mermaid** | Diagram DSL | Text‑based diagram language commonly used as an intermediate format for MCP‑driven diagram generation (including draw.io imports). | https://mermaid.js.org |

## License

MIT
