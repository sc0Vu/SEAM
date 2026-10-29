from engine.usage import RunRecord, Usage, format_usage, totals_by_role
from store import TaskStore
from tests.test_store import spec


def test_usage_adds_and_reports_cache_hit_rate():
    u = Usage(10, 80, 10, 5) + Usage(0, 100, 0, 1)
    assert u == Usage(10, 180, 10, 6)
    assert u.total_input == 200 and u.cache_hit_rate == 0.9
    assert Usage().cache_hit_rate == 0.0
    assert "hit 90%" in format_usage(u)


def test_totals_by_role():
    runs = [RunRecord(1, "worker", "w1", "diff", 0.2, Usage(1, 2, 3, 4)),
            RunRecord(2, "worker", "w2", "failed", 0.1, Usage(1, 0, 0, 1)),
            RunRecord(1, "reviewer", "r", "approve", 0.01, Usage(5, 0, 0, 2))]
    t = totals_by_role(runs)
    assert t["worker"][0] == 2 and round(t["worker"][1], 6) == 0.3
    assert t["worker"][2] == Usage(2, 2, 3, 5)
    assert t["reviewer"] == (1, 0.01, Usage(5, 0, 0, 2))


def test_store_round_trips_runs_per_task():
    store = TaskStore()
    a = store.create(spec(["a/**"]))
    b = store.create(spec(["b/**"]))
    store.record_run(RunRecord(a.id, "worker", "w1", "diff", 0.25, Usage(1, 2, 3, 4)))
    store.record_run(RunRecord(b.id, "gate", "gate", "reject"))
    assert store.runs(a.id) == [RunRecord(a.id, "worker", "w1", "diff", 0.25, Usage(1, 2, 3, 4))]
    assert [r.role for r in store.runs()] == ["worker", "gate"]


def test_store_migrates_an_old_runs_table_and_keeps_notes(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    TaskStore(db)  # current schema, then strip the column to mimic an old state dir
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE runs DROP COLUMN notes")
    con.execute("ALTER TABLE runs DROP COLUMN probes")
    con.commit()
    con.close()
    store = TaskStore(db)
    t = store.create(spec(["a/**"]))
    store.record_run(RunRecord(t.id, "reviewer", "r", "reject", notes="add tests",
                               probes='[{"name": "test_probe_a.py", "code": "x"}]'))
    assert store.runs(t.id)[0].notes == "add tests"
    assert "test_probe_a.py" in store.runs(t.id)[0].probes
