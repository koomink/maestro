import json

from maestro.monitoring.audit_logger import AuditLogger


def test_audit_logger_reads_latest_hash_from_large_log(tmp_path):
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    expected_hash = None

    for index in range(3000):
        audit.log("run_1", "event", {"index": index, "padding": "x" * 16})
        with audit.path.open(encoding="utf-8") as handle:
            expected_hash = json.loads(handle.readlines()[-1])["event_hash"]

    assert audit._latest_event_hash() == expected_hash


def test_audit_logger_reads_latest_hash_when_last_line_exceeds_tail_chunk(tmp_path):
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))

    audit.log("run_1", "first", {})
    audit.log("run_1", "large", {"padding": "x" * 9000})

    rows = [
        json.loads(line)
        for line in audit.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit._latest_event_hash() == rows[-1]["event_hash"]


def test_audit_logger_skips_trailing_blank_lines_when_reading_latest_hash(tmp_path):
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))

    audit.log("run_1", "first", {})
    latest_hash = audit._latest_event_hash()
    with audit.path.open("a", encoding="utf-8") as handle:
        handle.write("\n   \n")

    assert audit._latest_event_hash() == latest_hash


def test_audit_logger_preserves_previous_hash_chain(tmp_path):
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))

    audit.log("run_1", "first", {})
    audit.log("run_1", "second", {})
    audit.log("run_1", "third", {})

    rows = [
        json.loads(line)
        for line in audit.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["previous_hash"] is None
    assert rows[1]["previous_hash"] == rows[0]["event_hash"]
    assert rows[2]["previous_hash"] == rows[1]["event_hash"]
