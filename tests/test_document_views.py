import json
from pathlib import Path
import tempfile
import unittest
from eutonos.document_views import changes, boards_view, metadata

class DocumentViewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/"docs").mkdir()
    def write(self,records):
        (self.root/"docs/CHANGELOG.md").write_text("# Changes\n"+"\n".join("```tokenslang-change\n"+json.dumps(r)+"\n```" for r in records),encoding="utf-8")
    def item(self,**fields):
        return dict(profile="eutonos.tokenslang.change.v1",id="one",title="Change",summary="Observed result",status="completed",**fields)
    def test_absent_is_not_configured(self):
        self.assertEqual(changes(self.root)["status"],"not-configured")
    def test_configured_missing_owner_is_partial(self):
        (self.root/"eutonos.changes.json").write_text(json.dumps({"profile":"eutonos.tokenslang.changes.v1","owners":["docs/CHANGELOG.md"]}))
        self.assertEqual(changes(self.root)["status"],"partial")
    def test_valid_dates_tags_and_undated(self):
        item=self.item(date="2026-09-13",area="application",module="dashboard")
        self.write([item,{**self.item(),"id":"two"}]);v=changes(self.root)
        self.assertEqual(v["status"],"available");self.assertEqual(v["items"][0]["display"]["display_date"],"2026-09-13")
        self.assertIsNone(v["items"][1]["display"]["display_date"])
    def test_duplicate_and_invalid_dates_are_diagnostic(self):
        self.write([self.item(),self.item()]);self.assertEqual(changes(self.root)["status"],"partial")
        self.write([self.item(date="2026-02-30")]);self.assertEqual(changes(self.root)["status"],"partial")
    def test_unclosed_and_nested_example(self):
        p=self.root/"docs/CHANGELOG.md";p.write_text("````markdown\n```tokenslang-change\n{}\n```\n````\n",encoding="utf-8")
        self.assertEqual(changes(self.root)["items"],[])
        p.write_text("```tokenslang-change\n{}",encoding="utf-8");self.assertEqual(changes(self.root)["status"],"partial")
    def test_config_escape_and_invalid_shape(self):
        p=self.root/"eutonos.changes.json";p.write_text(json.dumps({"profile":"eutonos.tokenslang.changes.v1","owners":["../secret"]}))
        self.assertEqual(changes(self.root)["status"],"error")
        p.write_text("[]");self.assertEqual(changes(self.root)["status"],"error")
    def test_board_metadata_is_optional_and_invalid_is_partial(self):
        source={"status":"available","items":[{"id":"a","extensions":{"completed_on":"bad"}}]}
        result=boards_view(source);self.assertEqual(result["status"],"partial");self.assertNotIn("display",source["items"][0])
        self.assertIsNone(metadata({})["display_date"])
if __name__=="__main__":unittest.main()
