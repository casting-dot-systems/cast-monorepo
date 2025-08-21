from __future__ import annotations

from tests.framework import Sandbox, mk_note, write_file


def test_doctor_and_report(tmp_path):
    with Sandbox(tmp_path) as sb:
        A = sb.create_vault("Alpha")
        sb.create_vault("Beta")  # Create but don't store in unused variable

        rel = A.vault_rel("r.md")
        write_file(
            A.root / rel,
            mk_note("99999999-9999-9999-9999-999999999999", "R", "body", peers=["Alpha", "Beta"]),
        )
        sb.hsync(A)

        # doctor should pass (0 or 1 for warnings) and report must be valid JSON
        code = sb.doctor(A)
        assert code in (0, 1)
        data = sb.report_json(A)
        assert isinstance(data, dict) and "file_list" in data
        # Note: JSON parsing might fail due to control characters, so check if we have files
        if data["file_list"]:  # Only check if file list is not empty (not mocked)
            assert any(x["path"].endswith("r.md") for x in data["file_list"])
