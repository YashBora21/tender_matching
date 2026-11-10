"""Synthetic input sets: refresh preserves historical run bindings."""
from app.repositories.runs import tender_inputs, inputs_are_current
from app.repositories.tender import TenderRepo


def test_run_inputs_are_copied_and_new_requirement_ids_prevent_resume():
    rows = [{"id": "synthetic-old", "source_text": 'Size 2"'}]
    items = [{"item_id": "1", "name": "Synthetic valve"}]
    snapshot = {"tender_inputs": tender_inputs(rows, items, tender_id="synthetic")}
    rows[0]["source_text"] = "changed"
    items[0]["name"] = "changed"
    assert snapshot["tender_inputs"]["requirements"][0]["source_text"] == 'Size 2"'
    assert snapshot["tender_inputs"]["items"][0]["name"] == "Synthetic valve"
    assert inputs_are_current(snapshot, [{"id": "synthetic-old"}])
    assert not inputs_are_current(snapshot, [{"id": "synthetic-new"}])
    assert not inputs_are_current({}, rows)


def test_replacement_has_no_destructive_statements():
    class Result:
        def all(self): return []
    class Session:
        def __init__(self): self.statements = []
        def execute(self, statement, *args):
            self.statements.append(statement)
            return Result()
    session = Session()
    repo = TenderRepo(session)
    repo.replace_items("synthetic", [{"item_id": "1", "name": "Synthetic item"}])
    repo.replace_requirements("synthetic", [])
    assert not any(statement.is_delete for statement in session.statements)
    updates = [str(statement) for statement in session.statements if statement.is_update]
    assert any("superseded_at" in statement for statement in updates)
    assert any("active" in statement for statement in updates)
