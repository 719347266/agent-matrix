from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock
from agent_matrix.models import Config, MatrixError
from agent_matrix.records import read_records


class RecordReads(unittest.TestCase):
    def read(self, pages, config=None):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            c = config or Config(base_token="base_fixture", tables={"事项表": "tbl_fixture"})
            state = Mock(); state.get.return_value = "base_fixture"
            def export(argv, manifest=False):
                meta, rows = pages.pop(0)
                path = root / argv[argv.index("--output") + 1]
                path.write_text("\n".join(json.dumps(row) for row in rows))
                path.with_suffix(".manifest.json").write_text(json.dumps({
                    "base_token": "base_fixture", "table_id": "tbl_fixture", "rev": 1,
                    "columns": {"标题": {}}, "records_count": len(rows), "has_more": False, **meta}))
            client = SimpleNamespace(root=root, config=c, _exec=export)
            return read_records(client, state, "事项表", ["标题"])

    def test_complete_multi_page_read(self):
        rows = self.read([({"has_more": True, "next_offset": 1}, [{"record_id": "rec1", "标题": "一"}]),
                          ({}, [{"record_id": "rec2", "标题": "二"}])])
        self.assertEqual([r["record_id"] for r in rows], ["rec1", "rec2"])

    def test_changed_revision_refuses_partial_results(self):
        with self.assertRaises(MatrixError):
            self.read([({"has_more": True, "next_offset": 1}, [{"record_id": "rec1"}]),
                       ({"rev": 2}, [{"record_id": "rec2"}])])

    def test_missing_projection_or_wrong_table_or_invalid_offset_rejected(self):
        for meta in ({"columns": {}}, {"table_id": "another"}, {"records_count": 3},
                     {"has_more": True, "next_offset": 0}, {"has_more": "false"}):
            with self.subTest(meta=meta), self.assertRaises(MatrixError):
                self.read([(meta, [{"record_id": "rec1"}])])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(MatrixError):
            self.read([({}, [{"record_id": "same"}, {"record_id": "same"}])])


if __name__ == "__main__":
    unittest.main()
