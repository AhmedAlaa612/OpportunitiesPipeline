import unittest
from unittest.mock import Mock, patch

import numpy as np

from OpportunitiesPipeline import embed
from OpportunitiesPipeline.helpers import db


class UpsertOpportunityTests(unittest.TestCase):
    @patch("OpportunitiesPipeline.helpers.db.get_db_connection")
    def test_upsert_is_parameterized_and_closes_resources(self, get_connection):
        connection = Mock()
        cursor = connection.cursor.return_value
        get_connection.return_value = connection
        data_en = {
            "title": "Opportunity",
            "type": {"category": "academic", "subtype": ["masters"]},
            "country": ["Egypt"],
            "fund_type": ["fully_funded"],
            "target_segment": ["graduate"],
            "open_to_egyptians": True,
        }

        db.upsert_opportunity("00000000-0000-0000-0000-000000000001", data_en, {"title": "فرصة"})

        sql, params = cursor.execute.call_args.args
        self.assertIn("ON CONFLICT (id) DO UPDATE", sql)
        self.assertIn("%s::uuid", sql)
        self.assertNotIn("Opportunity", sql)
        self.assertEqual(len(params), 19)
        self.assertEqual(params[6:8], ("Opportunity", "فرصة"))
        connection.commit.assert_called_once()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    @patch("OpportunitiesPipeline.helpers.db.get_db_connection")
    def test_upsert_closes_resources_when_execution_fails(self, get_connection):
        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.execute.side_effect = RuntimeError("database failure")
        get_connection.return_value = connection

        with self.assertRaises(RuntimeError):
            db.upsert_opportunity("00000000-0000-0000-0000-000000000001", {}, {})

        cursor.close.assert_called_once()
        connection.close.assert_called_once()


class EmbedOpportunityTests(unittest.TestCase):
    def test_build_point_preserves_search_payload_and_active_state(self):
        opportunity = {
            "id": "00000000-0000-0000-0000-000000000001",
            "title": "Opportunity",
            "type": {"category": "academic", "subtype": ["masters"]},
            "fund_type": ["fully_funded"],
            "target_segment": ["graduate"],
            "deadline": "2999-12-31",
            "is_active": False,
        }

        point = embed.build_qdrant_point(opportunity, np.array([0.1, 0.2]))

        self.assertEqual(point.id, opportunity["id"])
        self.assertEqual(
            set(point.payload),
            {
                "program_id", "title", "country", "fund_type", "category", "subtype",
                "documents_required", "exam_scores", "is_remote", "target_segment",
                "deadline", "deadline_ts", "is_active", "gpa",
                "has_language_requirements", "has_fee", "has_document_requirements",
            },
        )
        self.assertFalse(point.payload["is_active"])

    @patch("OpportunitiesPipeline.embed.time.sleep")
    @patch("OpportunitiesPipeline.embed.QdrantClient")
    @patch("OpportunitiesPipeline.embed.get_jina_embedding")
    def test_embed_and_upsert_uses_input_id(self, get_embedding, qdrant_client, _sleep):
        opportunity_id = "00000000-0000-0000-0000-000000000001"
        get_embedding.return_value = np.array([[0.1, 0.2]], dtype=np.float32)
        opportunity = {"id": opportunity_id, "title": "Opportunity", "description": "Details"}

        count = embed.embed_and_upsert([opportunity])

        self.assertEqual(count, 1)
        points = qdrant_client.return_value.upsert.call_args.kwargs["points"]
        self.assertEqual(points[0].id, opportunity_id)
        self.assertTrue(points[0].payload["is_active"])


if __name__ == "__main__":
    unittest.main()