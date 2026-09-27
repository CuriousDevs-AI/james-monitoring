import pytest

from james_monitoring.tasks import TaskError, TaskStore


def store(tmp_path):
    return TaskStore(tmp_path, "UTC", owner_id="pankaj")


def test_create_and_roundtrip(tmp_path):
    s = store(tmp_path)
    t = s.create(title="Runtime survey", owner="alex", created_by="pankaj", priority="P0", due="2099-01-02",
                 done_means=["note written", "3 options compared"])
    assert t.id == "T-001" and t.path.name.startswith("T-001-runtime-survey")
    t2 = s.get("t-001")
    assert t2.priority == "P0" and t2.status == "todo"
    assert "- [ ] note written" in t2.doc.sections["Done means"]
    assert s.create(title="x", owner="alex", created_by="pankaj").id == "T-002"


def test_one_p0_per_person_for_agents_but_owner_can_override(tmp_path):
    s = store(tmp_path)
    s.create(title="a", owner="alex", created_by="pankaj", priority="P0")
    with pytest.raises(TaskError, match="already has a P0"):
        s.create(title="b", owner="alex", created_by="james", priority="P0")
    s.create(title="c", owner="alex", created_by="pankaj", priority="P0")   # founder decides


def test_doing_needs_done_means_and_wip_limit(tmp_path):
    s = store(tmp_path)
    t = s.create(title="no criteria", owner="alex", created_by="pankaj")
    with pytest.raises(TaskError, match="Done means"):
        s.set_status(t.id, "doing", by="alex")
    ids = [s.create(title=f"t{i}", owner="alex", created_by="pankaj", done_means=["x"]).id for i in range(3)]
    s.set_status(ids[0], "doing", by="alex")
    s.set_status(ids[1], "doing", by="alex")
    with pytest.raises(TaskError, match="already has 2"):
        s.set_status(ids[2], "doing", by="alex")


def test_only_reviewer_marks_done_and_blocked_needs_reason(tmp_path):
    s = store(tmp_path)
    t = s.create(title="x", owner="alex", created_by="pankaj", done_means=["y"])
    t, msg = s.set_status(t.id, "done", by="alex")
    assert t.status == "review" and "Only pankaj" in msg
    with pytest.raises(TaskError, match="blocked_on"):
        s.set_status(t.id, "blocked", by="alex")
    t, _ = s.set_status(t.id, "blocked", by="alex", blocked_on="pankaj — Jetson yes/no")
    assert t.blocked_on.startswith("pankaj")
    t, _ = s.set_status(t.id, "done", by="pankaj")
    assert t.status == "done" and t.doc.meta["done_on"]


def test_person_status_and_board(tmp_path):
    s = store(tmp_path)
    assert s.person_status("alex")[0] == "not started"
    t = s.create(title="late", owner="alex", created_by="pankaj", due="2000-01-01", done_means=["x"])
    assert s.person_status("alex")[0] == "at risk"
    s.set_status(t.id, "blocked", by="alex", blocked_on="marcus — schema")
    assert s.person_status("alex")[0] == "blocked"
    assert "BLOCKED (1)" in s.board()
