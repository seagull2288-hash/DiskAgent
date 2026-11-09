# Upstream reference and adaptation

- Repository: https://github.com/hongwenwang36-eng/ANSYS-Workbench-mcp
- Pinned commit: `2950add0f46023c685d897135970c9e691b2f255`
- License: MIT; full original notice is retained in `LICENSE` beside this file.
- Upstream's README reports testing with ANSYS 2025 R1. It does not establish compatibility with this user's ANSYS 2020 R2.

DiskAgent adapts the upstream **FastMCP stdio → asynchronous command/result bridge → Workbench Journal** architecture.
The `_path_state` and `_state_changed` helpers from upstream `mcp_server.py` are adapted in `agent_app/vendor/workbench_mcp.py`, with an additional nonempty regular-file requirement. Those helpers are used for the dedicated single-design result artifact; existing native solver checks remain authoritative.

The upstream generic file-IPC daemon and its 50 broad CAD/CFD tools are replaced by DiskAgent's local HTTP worker queue and typed engineering capabilities. The reason is concrete: this project already has a verified v202 Journal, a fixed nine-parameter mapping, original mechanical result verification, mass/modal profiles, a cross-process project lock, budgeted workflow, and constrained recovery. A second independent script launcher could bypass those checks or race the current app.

Changes relative to the upstream generic bridge:

1. Registered model and bounded nine-dimensional evaluation; caller cannot supply arbitrary script, executable, file or project path.
2. Same serialized app worker for browser/MCP jobs plus existing cross-process native project lock.
3. Async job IDs, durable result records, request-id idempotency and argument conflict detection.
4. Preview defaults and explicit live budgets; planner LLM disabled for structured task submission.
5. Original verified CSV/native Mechanical evidence and optional mass/frequency collection.
6. Compact reports preserve constraints and feasible recommendations; no new solve to read a report.

This is a project-specific adaptation, not a full upstream fork or a claim that all upstream tools work in v202. No arbitrary natural-language CAD creation is implemented in this first integration.
