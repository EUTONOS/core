import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eutonos import companion_attribution
from eutonos import delivery_summary_cli


class DeliveryControlCliTests(unittest.TestCase):
    def test_delivery_cli_passes_explicit_authority(self):
        projected = {"profile": "eutonos.delivery-summary.v1"}
        with patch.object(delivery_summary_cli, "project", return_value=projected) as reader, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(delivery_summary_cli.main(["--root", ".", "--commit-authority", "yes",
                                                        "--publish-authority", "no"]), 0)
        self.assertEqual(json.loads(output.getvalue()), projected)
        self.assertEqual(reader.call_args.kwargs["authority"]["commit"]["authorized"], True)
        self.assertEqual(reader.call_args.kwargs["authority"]["publish"]["authorized"], False)

    def test_relationship_receipt_store_is_idempotent_and_conflict_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = {
                "profile": companion_attribution.RECEIPT, "receipt_id": "receipt-1",
                "request_id": "request-1", "assignment_id": "assignment-1",
                "primary": {"thread_id": "primary", "turn_id": "turn-1"},
                "worker": {"thread_id": "worker", "turn_id": "turn-2"},
                "observed_worker": {"model": "gpt-6-luna", "effort": "high", "basis": "native host"},
                "result": {"status": "completed", "basis": "native terminal state"},
                "review": {"status": "accepted", "turns": [], "basis": "primary review"},
                "phases": {name: None for name in companion_attribution.PHASES},
            }
            self.assertEqual(companion_attribution.record(root, receipt)["outcome"], "recorded")
            self.assertEqual(companion_attribution.record(root, receipt)["outcome"], "idempotent")
            self.assertEqual(len(companion_attribution.receipts(root)), 1)
            changed = json.loads(json.dumps(receipt))
            changed["result"]["status"] = "needs-attention"
            with self.assertRaises(Exception) as raised:
                companion_attribution.record(root, changed)
            self.assertEqual(getattr(raised.exception, "code", None), "conflict")


if __name__ == "__main__":
    unittest.main()
