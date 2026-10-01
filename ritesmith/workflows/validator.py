"""WorkflowValidator — validates Trama workflow definitions.

Understands two formats:
  v1 (legacy): flat "steps" list with step_id / step_type / depends_on
  v2 (current): "entrypoint" + "nodes" list with id / kind / next

Layer 1 — Structural:
  - Required fields present and typed correctly
  - All node references (next, target, default, entrypoint) resolve to real ids
  - switch nodes have a "default"
  - split/join follow Trama's rules: a split fans out into >= 2 branches and owns
    exactly one join; nothing else may route into a join; branch nodes' outputs are
    only visible after the join through nodes.<join>.response.body.branches
  - No reachability cycles from entrypoint

Layer 2 — Contracts (only when capability_registry is provided):
  - capability_name in action.request.body must exist in registry
  - {{ nodes.X.* }} template references must point to real node ids
"""

from __future__ import annotations

import json
import re

_V1_STEP_TYPES = {"capability", "sleep", "condition", "notification", "callback"}
_V2_NODE_KINDS = {"task", "switch", "sleep", "split", "join"}
_NODE_REF = re.compile(r"\bnodes\.([A-Za-z0-9_\-]+)")


def _v2_successors(node: dict | None) -> list[str]:
    """Outgoing edges of a v2 node, excluding the "end" terminal."""
    if not node:
        return []
    kind = node.get("kind")
    if kind in ("task", "sleep", "join"):
        targets = [node.get("next") or "end"]
    elif kind == "switch":
        targets = [c.get("target", "end") for c in node.get("cases", [])] + [
            node.get("default", "end")
        ]
    elif kind == "split":
        targets = list(node.get("branches") or []) + [node.get("join") or "end"]
    else:
        targets = []
    return [t for t in targets if t and t != "end"]


def _branch_sets(nodes: list) -> dict[str, set[str]]:
    """Node ids of each split branch, keyed by the branch's entry node id.

    A branch starts at one of the split's `branches` entries and runs until a
    terminal ("next": "end") — it never flows into the join; Trama resumes the
    parent at the join once every branch has finished.
    """
    node_map = {n.get("id"): n for n in nodes if isinstance(n, dict) and n.get("id")}
    sets: dict[str, set[str]] = {}
    for node in node_map.values():
        if node.get("kind") != "split":
            continue
        for entry in node.get("branches") or []:
            if entry not in node_map:
                continue
            members: set[str] = set()
            stack = [entry]
            while stack:
                nid = stack.pop()
                if nid in members:
                    continue
                members.add(nid)
                current = node_map[nid]
                nexts = (
                    list(current.get("branches") or [])  # nested split: its branches stay inside
                    if current.get("kind") == "split"
                    else _v2_successors(current)
                )
                stack.extend(t for t in nexts if t in node_map)
            sets[entry] = members
    return sets


def branch_node_ids(nodes: list) -> set[str]:
    """Ids of nodes that run inside a split branch (a child execution)."""
    return set().union(*_branch_sets(nodes).values()) if nodes else set()


class WorkflowValidator:
    def __init__(self, capability_registry: dict[str, dict] | None = None):
        """
        capability_registry: capability_id → {input_schema, output_schema, ...}
        If None, Layer 2 contract checking is skipped.
        """
        self.capability_registry = capability_registry or {}

    def validate(self, definition: dict) -> list[str]:
        if not isinstance(definition, dict):
            return ["definition must be a JSON object"]

        if "nodes" in definition or "entrypoint" in definition:
            return self._validate_v2(definition)
        return self._validate_v1(definition)

    # ------------------------------------------------------------------
    # v2 format: entrypoint + nodes
    # ------------------------------------------------------------------

    def _validate_v2(self, definition: dict) -> list[str]:
        errors: list[str] = []

        nodes_list = definition.get("nodes", [])
        if not isinstance(nodes_list, list):
            return ["definition.nodes must be a list"]

        node_ids: set[str] = set()
        for node in nodes_list:
            nid = node.get("id")
            if not nid:
                errors.append("Node missing 'id'")
            else:
                if nid in node_ids:
                    errors.append(f"Duplicate node id: '{nid}'")
                node_ids.add(nid)

        entrypoint = definition.get("entrypoint")
        if not entrypoint:
            errors.append("definition.entrypoint is required")
        elif entrypoint not in node_ids and entrypoint != "end":
            errors.append(f"entrypoint '{entrypoint}' does not reference a known node")

        if errors:
            return errors

        for node in nodes_list:
            errors.extend(self._validate_v2_node(node, node_ids))

        if not errors:
            errors.extend(self._validate_split_join(nodes_list))

        if not errors:
            max_iterations = definition.get("max_iterations")
            if max_iterations is not None:
                if not isinstance(max_iterations, int) or max_iterations < 1:
                    errors.append("max_iterations must be a positive integer when present")
            else:
                errors.extend(self._detect_v2_cycles(nodes_list, entrypoint or "", node_ids))
            errors.extend(self._layer2_v2(nodes_list, node_ids))

        return errors

    def _validate_v2_node(self, node: dict, node_ids: set[str]) -> list[str]:
        errors: list[str] = []
        nid = node.get("id", "<unknown>")
        kind = node.get("kind")

        if kind not in _V2_NODE_KINDS:
            errors.append(
                f"Node '{nid}': invalid kind '{kind}' (must be one of {sorted(_V2_NODE_KINDS)})"
            )
            return errors

        if kind == "task":
            if not node.get("action"):
                errors.append(f"Node '{nid}': task node must have an 'action'")
            next_id = node.get("next")
            if not next_id:
                errors.append(
                    f"Node '{nid}': task node must have a 'next' (use 'end' to terminate)"
                )
            elif next_id != "end" and next_id not in node_ids:
                errors.append(f"Node '{nid}': 'next' references non-existent node '{next_id}'")
            mode = (node.get("action") or {}).get("mode", "")
            if mode == "async-http-callback":
                callback = (node.get("action") or {}).get("callback", {})
                if not callback.get("timeoutMillis"):
                    errors.append(
                        f"Node '{nid}': async-http-callback action must specify callback.timeoutMillis"
                    )
                if not callback.get("successWhen"):
                    errors.append(
                        f"Node '{nid}': async-http-callback action must specify callback.successWhen"
                    )

        elif kind == "switch":
            cases = node.get("cases")
            if not cases or not isinstance(cases, list):
                errors.append(f"Node '{nid}': switch node must have a non-empty 'cases' list")
            else:
                for case in cases:
                    target = case.get("target")
                    if not target:
                        errors.append(f"Node '{nid}': switch case missing 'target'")
                    elif target != "end" and target not in node_ids:
                        errors.append(f"Node '{nid}': switch case target '{target}' does not exist")
                    if not case.get("when"):
                        errors.append(f"Node '{nid}': switch case missing 'when' condition")
            default = node.get("default")
            if not default:
                errors.append(f"Node '{nid}': switch node must have a 'default'")
            elif default != "end" and default not in node_ids:
                errors.append(
                    f"Node '{nid}': switch 'default' references non-existent node '{default}'"
                )

        elif kind == "split":
            branches = node.get("branches")
            if not isinstance(branches, list) or len(branches) < 2:
                errors.append(f"Node '{nid}': split node must have at least 2 'branches'")
            else:
                for branch in branches:
                    if branch not in node_ids:
                        errors.append(f"Node '{nid}': split branch '{branch}' does not exist")
            join = node.get("join")
            if not join:
                errors.append(f"Node '{nid}': split node must name its 'join' node")
            elif join not in node_ids:
                errors.append(f"Node '{nid}': split 'join' references non-existent node '{join}'")

        elif kind == "join":
            next_id = node.get("next")
            if next_id and next_id != "end" and next_id not in node_ids:
                errors.append(f"Node '{nid}': 'next' references non-existent node '{next_id}'")

        elif kind == "sleep":
            duration = node.get("durationSeconds")
            if duration is None:
                errors.append(f"Node '{nid}': sleep node must specify 'durationSeconds'")
            elif not isinstance(duration, (int, float)) or duration <= 0:
                errors.append(
                    f"Node '{nid}': sleep node 'durationSeconds' must be a positive number"
                )
            next_id = node.get("next")
            if not next_id:
                errors.append(f"Node '{nid}': sleep node must have a 'next'")
            elif next_id != "end" and next_id not in node_ids:
                errors.append(f"Node '{nid}': 'next' references non-existent node '{next_id}'")

        return errors

    def _validate_split_join(self, nodes: list) -> list[str]:
        """Trama's split/join rules, plus the data-visibility rule for branch outputs."""
        errors: list[str] = []
        node_map = {n["id"]: n for n in nodes if n.get("id")}
        splits = [n for n in nodes if n.get("kind") == "split"]
        joins = {n["id"] for n in nodes if n.get("kind") == "join"}

        owners: dict[str, list[str]] = {}
        for split in splits:
            join = split.get("join")
            if join in node_map and join not in joins:
                errors.append(f"Node '{split['id']}': split 'join' must reference a join node")
            owners.setdefault(join, []).append(split["id"])
        for join in sorted(joins):
            owned_by = owners.get(join, [])
            if len(owned_by) != 1:
                errors.append(
                    f"Join '{join}' must be owned by exactly one split (owned by {owned_by or 'none'})"
                )

        # Only the owning split's `join` field may point at a join; branches never flow into it.
        for node in nodes:
            for target in _v2_successors(node):
                is_own_join = node.get("kind") == "split" and node.get("join") == target
                if target in joins and not is_own_join:
                    errors.append(
                        f"Node '{node['id']}': must not route into join '{target}' — end the "
                        f'branch with "next": "end"; Trama resumes at the join automatically'
                    )

        # Each branch runs as a child execution that starts with only the payload: it sees
        # its own nodes, not the parent's; the parent sees branch results only via the join.
        branch_sets = _branch_sets(nodes)
        inside = set().union(*branch_sets.values()) if branch_sets else set()
        for node in nodes:
            refs = set(_NODE_REF.findall(json.dumps(node))) & set(node_map)
            if node["id"] not in inside:
                for ref in sorted(refs & inside):
                    errors.append(
                        f"Node '{node['id']}': references branch node '{ref}', whose output is "
                        f"not visible outside its branch — read it from "
                        f"nodes.<join>.response.body.branches[].result instead"
                    )
                continue
            same_branch = set().union(
                *(members for members in branch_sets.values() if node["id"] in members)
            )
            for ref in sorted(refs - same_branch):
                errors.append(
                    f"Node '{node['id']}': runs inside a split branch and cannot see node "
                    f"'{ref}' — a branch only has the payload and its own nodes"
                )
        return errors

    def _detect_v2_cycles(self, nodes: list, entrypoint: str, node_ids: set[str]) -> list[str]:
        node_map: dict[str, dict] = {n["id"]: n for n in nodes if n.get("id")}

        def successors(nid: str) -> list[str]:
            return _v2_successors(node_map.get(nid))

        visited: set[str] = set()
        in_stack: set[str] = set()

        def dfs(nid: str) -> bool:
            if nid not in node_ids:
                return False
            visited.add(nid)
            in_stack.add(nid)
            for s in successors(nid):
                if s not in visited:
                    if dfs(s):
                        return True
                elif s in in_stack:
                    return True
            in_stack.discard(nid)
            return False

        if entrypoint in node_ids and dfs(entrypoint):
            return ["Cycle detected in workflow node graph"]
        return []

    def _layer2_v2(self, nodes: list, node_ids: set[str]) -> list[str]:
        errors: list[str] = []
        if not self.capability_registry:
            return errors

        available = sorted(self.capability_registry.keys())

        for node in nodes:
            if node.get("kind") != "task":
                continue
            nid = node.get("id", "<unknown>")
            body = (node.get("action") or {}).get("request", {}).get("body") or {}
            cap_name = body.get("capability_name")

            if cap_name and cap_name not in self.capability_registry:
                errors.append(
                    f"Node '{nid}': unknown capability_name '{cap_name}'. Available: {available}"
                )

            errors.extend(self._validate_templates(nid, body, node_ids))

        return errors

    def _validate_templates(self, nid: str, body: dict, node_ids: set[str]) -> list[str]:
        errors = []
        body_str = json.dumps(body)
        for ref_node in re.findall(r"\{\{\s*nodes\.([^.\s}]+)", body_str):
            if ref_node not in node_ids:
                errors.append(f"Node '{nid}': template references undefined node '{ref_node}'")
        return errors

    # ------------------------------------------------------------------
    # v1 format: flat steps list (backward compat)
    # ------------------------------------------------------------------

    def _validate_v1(self, definition: dict) -> list[str]:
        errors: list[str] = []
        steps = definition.get("steps", [])
        if not isinstance(steps, list):
            return ["definition.steps must be a list"]

        step_ids = {s.get("step_id") for s in steps if isinstance(s, dict) and s.get("step_id")}

        for step in steps:
            sid = step.get("step_id")
            if not sid:
                errors.append("Step missing step_id")
                continue
            step_type = step.get("step_type")
            if step_type not in _V1_STEP_TYPES:
                errors.append(
                    f"Step '{sid}': invalid step_type '{step_type}' "
                    f"(must be one of {sorted(_V1_STEP_TYPES)})"
                )
            for dep in step.get("depends_on", []):
                if dep not in step_ids:
                    errors.append(f"Step '{sid}': depends_on '{dep}' which does not exist")
            if step_type == "capability" and not step.get("capability_id"):
                errors.append(f"Step '{sid}': capability step must have a capability_id")

        if not errors:
            errors.extend(self._detect_v1_cycles(steps))
        if not errors:
            errors.extend(self._layer2_v1(steps, step_ids))

        return errors

    def _detect_v1_cycles(self, steps: list) -> list[str]:
        graph: dict[str, list[str]] = {
            s["step_id"]: s.get("depends_on", []) for s in steps if s.get("step_id")
        }
        visited: set[str] = set()
        in_stack: set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            in_stack.add(node)
            for n in graph.get(node, []):
                if n not in visited:
                    if dfs(n):
                        return True
                elif n in in_stack:
                    return True
            in_stack.discard(node)
            return False

        for sid in graph:
            if sid not in visited and dfs(sid):
                return [f"Dependency cycle detected involving step '{sid}'"]
        return []

    def _layer2_v1(self, steps: list, step_ids: set[str]) -> list[str]:
        errors: list[str] = []
        if not self.capability_registry:
            return errors

        step_output_schemas: dict[str, dict] = {}
        for step in steps:
            cap_id = step.get("capability_id")
            if cap_id and cap_id in self.capability_registry:
                step_output_schemas[step["step_id"]] = (
                    self.capability_registry[cap_id].get("output_schema") or {}
                )

        for step in steps:
            sid = step.get("step_id", "<unknown>")
            cap_id = step.get("capability_id")
            if step.get("step_type") != "capability" or not cap_id:
                continue
            cap = self.capability_registry.get(cap_id)
            if not cap:
                errors.append(f"Step '{sid}': capability_id '{cap_id}' not found in registry")
                continue
            for field, wiring in (step.get("inputs") or {}).items():
                if not isinstance(wiring, dict):
                    continue
                from_step = wiring.get("from_step")
                from_field = wiring.get("from_field")
                if from_step and from_step not in step_ids:
                    errors.append(
                        f"Step '{sid}': input '{field}' references non-existent step '{from_step}'"
                    )
                elif from_step and from_field:
                    src_props = step_output_schemas.get(from_step, {}).get("properties", {})
                    if src_props and from_field not in src_props:
                        errors.append(
                            f"Step '{sid}': input '{field}' references field '{from_field}' "
                            f"not present in step '{from_step}' output schema"
                        )
        return errors
