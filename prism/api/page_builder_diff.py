"""Structural diff for Page Builder layout JSON.

The layout is a recursive tree of blocks, each with a stable ``id`` such as
``NAVBAR-1780572988940-372``. We flatten both the old and new trees into
``id -> node`` maps and compare them, so a change is reported against the
exact block (and, for edits, the exact field path within its ``props``).

Everything here is pure: no DB access, no Frappe imports. It takes two parsed
layouts and returns plain dicts describing the change rows and a summary.
"""

import json

# Keep individual value cells readable in the child grid; the full state always
# lives in the version snapshot, so truncating display values loses nothing.
MAX_VALUE_LEN = 300


def diff_layouts(old_raw, new_raw):
	"""Compare two layouts. Returns {rows, summary, change_count}.

	``rows`` is a list of dicts with keys: block_id, block_type, block_path,
	change_kind (Added|Removed|Moved|Edited), field, old_value, new_value.
	``change_count`` counts affected *blocks* (not rows).
	"""

	old_nodes = _flatten(_blocks(old_raw))
	new_nodes = _flatten(_blocks(new_raw))

	old_ids = set(old_nodes)
	new_ids = set(new_nodes)

	added_ids = new_ids - old_ids
	removed_ids = old_ids - new_ids
	common_ids = old_ids & new_ids

	rows = []
	affected = set()

	# Preserve document order for readability: added/edited/moved follow the new
	# tree order; removed follow the old tree order.
	for bid, node in new_nodes.items():
		if bid in added_ids:
			rows.append(_row(node, "Added", "", "", "(block added)"))
			affected.add(bid)

	for bid, node in old_nodes.items():
		if bid in removed_ids:
			rows.append(_row(node, "Removed", "", "(block removed)", ""))
			affected.add(bid)

	for bid, new_node in new_nodes.items():
		if bid not in common_ids:
			continue
		old_node = old_nodes[bid]

		# Moved: position within siblings or the parent itself changed.
		if (old_node["parent_id"] != new_node["parent_id"]
				or old_node["index"] != new_node["index"]):
			rows.append(_row(
				new_node, "Moved",
				"",
				_position_label(old_node),
				_position_label(new_node),
			))
			affected.add(bid)

		# Edited: any prop field changed (children handled by the tree walk).
		for path, old_v, new_v in _deep_diff(old_node["props"], new_node["props"], "props"):
			rows.append(_row(new_node, "Edited", path, old_v, new_v))
			affected.add(bid)

	summary = _summarise(rows, affected)
	return {"rows": rows, "summary": summary, "change_count": len(affected)}


# --- flattening ---------------------------------------------------------------

def _blocks(raw):
	"""Return the top-level block array from a layout value.

	Accepts either the wrapping object ``{"layout": [...]}`` or a bare list.
	Strings are parsed as JSON. Anything unexpected yields an empty list.
	"""
	if raw is None or raw == "":
		return []
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except (ValueError, TypeError):
			return []
	if isinstance(raw, dict):
		raw = raw.get("layout", [])
	return raw if isinstance(raw, list) else []


def _flatten(blocks, parent_id=None, parent_path=None, out=None):
	"""Walk the block tree depth-first into an ordered ``id -> node`` dict."""
	if out is None:
		out = {}
	parent_path = parent_path or []

	for index, block in enumerate(blocks):
		if not isinstance(block, dict):
			continue
		bid = block.get("id") or f"__anon_{parent_id}_{index}"
		btype = block.get("type", "")
		props = block.get("props")
		if not isinstance(props, dict):
			props = {}
		label = _label(block)
		path = parent_path + [label]

		out[bid] = {
			"id": bid,
			"type": btype,
			"props": props,
			"parent_id": parent_id,
			"index": index,
			"path": path,
		}

		children = block.get("children")
		if isinstance(children, list):
			_flatten(children, bid, path, out)

	return out


def _label(block):
	"""Best human label for a block: a meaningful prop, else its type."""
	props = block.get("props") if isinstance(block, dict) else None
	if not isinstance(props, dict):
		props = {}
	for key in ("label", "heading", "title", "text", "brandName"):
		val = props.get(key)
		if isinstance(val, str) and val.strip():
			return val.strip()
	return block.get("type", "block")


# --- field-level diff ---------------------------------------------------------

def _deep_diff(old, new, prefix):
	"""Yield (path, old_value, new_value) for scalar changes between two values.

	Recurses through dicts and lists (lists compared by index). Nested block
	trees never reach here because ``children`` is stripped before flattening.
	"""
	out = []

	if isinstance(old, dict) or isinstance(new, dict):
		old = old if isinstance(old, dict) else {}
		new = new if isinstance(new, dict) else {}
		for key in list(old.keys()) + [k for k in new.keys() if k not in old]:
			out.extend(_deep_diff(old.get(key), new.get(key), f"{prefix}.{key}"))
		return out

	if isinstance(old, list) or isinstance(new, list):
		old = old if isinstance(old, list) else []
		new = new if isinstance(new, list) else []
		for i in range(max(len(old), len(new))):
			ov = old[i] if i < len(old) else None
			nv = new[i] if i < len(new) else None
			out.extend(_deep_diff(ov, nv, f"{prefix}[{i}]"))
		return out

	if old != new:
		out.append((prefix, _fmt(old), _fmt(new)))
	return out


# --- helpers ------------------------------------------------------------------

def _row(node, kind, field, old_value, new_value):
	return {
		"block_id": node["id"],
		"block_type": node["type"],
		"block_path": " › ".join(str(p) for p in node["path"]),
		"change_kind": kind,
		"field": field,
		"old_value": old_value,
		"new_value": new_value,
	}


def _position_label(node):
	return f"{node['parent_id'] or 'root'} #{node['index']}"


def _fmt(value):
	if value is None:
		return ""
	if not isinstance(value, str):
		value = json.dumps(value, ensure_ascii=False)
	if len(value) > MAX_VALUE_LEN:
		return value[:MAX_VALUE_LEN] + "…"
	return value


def _summarise(rows, affected):
	if not rows:
		return "No block changes"
	kinds = {}
	seen = {}
	for r in rows:
		# Count each block once per kind for the headline.
		key = (r["block_id"], r["change_kind"])
		if key in seen:
			continue
		seen[key] = True
		kinds[r["change_kind"]] = kinds.get(r["change_kind"], 0) + 1

	order = ["Edited", "Added", "Removed", "Moved"]
	parts = [f"{kinds[k]} {k.lower()}" for k in order if kinds.get(k)]
	return ", ".join(parts)
