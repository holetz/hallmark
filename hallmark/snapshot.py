"""The connector contract: a snapshot is a plain dict, validated before it touches the store.

    {
      "tables": [{"fqn": "cat.schema.table", "comment": str, "last_altered": str,
                  "tags": {k: v}, "columns": [{"name": str, "type": str, "comment": str,
                                                "tags": {k: v}}]}],
      "models": [{"fqn": str, "comment": str, "last_altered": str,
                  "versions": [{"version": str, "status": str, "comment": str}]}],
      "features": [{"fqn": str, "table_fqn": str, "columns": [str]}],
      "lineage": [{"kind": "table" | "column", "src": str, "dst": str}],
    }

Only `tables` is required; `last_altered` must compare equal when nothing changed. A connector is
any callable returning this dict.
"""


class SnapshotError(ValueError):
    """The snapshot does not match the contract."""


def _text(value, where):
    if not isinstance(value, str) or not value:
        raise SnapshotError(f"{where}: expected a non-empty string")
    return value


def _tags(value, where):
    if value is None:
        return
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise SnapshotError(f"{where}: tags must map string to string")


def validate(snapshot):
    """Return the snapshot when it conforms, else raise SnapshotError naming the first fault."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tables"), list):
        raise SnapshotError("snapshot: 'tables' must be a list")
    for table in snapshot["tables"]:
        fqn = _text(table.get("fqn"), "table.fqn")
        if fqn.count(".") != 2:
            raise SnapshotError(f"table {fqn}: fqn must be catalog.schema.table")
        _text(table.get("last_altered"), f"table {fqn}.last_altered")
        _tags(table.get("tags"), f"table {fqn}")
        if not isinstance(table.get("columns"), list):
            raise SnapshotError(f"table {fqn}: 'columns' must be a list")
        names = set()
        for col in table["columns"]:
            name = _text(col.get("name"), f"table {fqn} column name")
            if name in names:
                raise SnapshotError(f"table {fqn}: duplicate column {name}")
            names.add(name)
            _tags(col.get("tags"), f"column {fqn}.{name}")
    for model in snapshot.get("models", []):
        _text(model.get("fqn"), "model.fqn")
        for version in model.get("versions", []):
            _text(str(version.get("version", "")), f"model {model['fqn']} version")
    for feature in snapshot.get("features", []):
        _text(feature.get("fqn"), "feature.fqn")
        _text(feature.get("table_fqn"), f"feature {feature['fqn']}.table_fqn")
    for edge in snapshot.get("lineage", []):
        if edge.get("kind") not in ("table", "column"):
            raise SnapshotError(f"lineage edge {edge}: kind must be 'table' or 'column'")
        _text(edge.get("src"), "lineage.src")
        _text(edge.get("dst"), "lineage.dst")
    return snapshot
