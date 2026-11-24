import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from OpportunitiesPipeline import api


class ManualApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        self.secret = "test-webhook-secret"
        self.secret_patcher = patch.object(api, "FORM_WEBHOOK_SECRET", self.secret)
        self.secret_patcher.start()
        self.db_patcher = patch.object(api, "upsert_opportunity")
        self.embed_patcher = patch.object(api, "embed_and_upsert")
        self.upsert = self.db_patcher.start()
        self.embed = self.embed_patcher.start()

    def tearDown(self):
        self.secret_patcher.stop()
        self.db_patcher.stop()
        self.embed_patcher.stop()

    @staticmethod
    def valid_payload():
        return {
            "title_en": "Graduate Research Award",
            "title_ar": "جائزة البحث للدراسات العليا",
            "description_en": "A funded research award for graduate students.",
            "description_ar": "جائزة بحثية ممولة لطلاب الدراسات العليا.",
            "application_link": "https://example.org/apply",
            "fund_type": ["fully_funded"],
            "category": "academic",
            "subtype": ["masters"],
            "target_segment": ["graduate"],
            "eligibility_en": "Graduate students may apply.",
            "eligibility_ar": "يمكن لطلاب الدراسات العليا التقديم.",
            "country": ["United States", "Egypt"],
            "deadline": "2027-01-31",
            "is_remote": False,
            "open_to_egyptians": True,
        }

    def test_health(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_missing_or_invalid_secret_returns_401_without_ingestion(self):
        for headers in ({}, {"X-Webhook-Secret": "wrong-secret"}):
            with self.subTest(headers=headers):
                response = self.client.post(
                    "/api/v1/opportunities/manual",
                    json=self.valid_payload(),
                    headers=headers,
                )
                self.assertEqual(response.status_code, 401)

        self.upsert.assert_not_called()
        self.embed.assert_not_called()

    def test_unconfigured_secret_returns_401(self):
        with patch.object(api, "FORM_WEBHOOK_SECRET", None):
            response = self.client.post(
                "/api/v1/opportunities/manual",
                json=self.valid_payload(),
                headers={"X-Webhook-Secret": self.secret},
            )

        self.assertEqual(response.status_code, 401)
        self.upsert.assert_not_called()
        self.embed.assert_not_called()

    def test_critical_fields_are_rejected_before_ingestion(self):
        critical_fields = (
            "title_en",
            "title_ar",
            "description_en",
            "description_ar",
            "application_link",
            "fund_type",
            "category",
            "subtype",
            "target_segment",
        )
        invalid_values = {
            "title_en": (None, "", "   ", "x"),
            "title_ar": (None, "", "   ", "x"),
            "description_en": (None, "", "   ", "too short"),
            "description_ar": (None, "", "   ", "too short"),
            "application_link": (None, "", "   ", "not-a-url"),
            "fund_type": (None, [], ["   "], ["unknown"]),
            "category": (None, "", "   ", "unknown"),
            "subtype": (None, [], ["   "], ["unknown"]),
            "target_segment": (None, [], ["   "], ["unknown"]),
        }

        for field in critical_fields:
            for value in invalid_values[field]:
                with self.subTest(field=field, value=value):
                    body = self.valid_payload()
                    if value is None:
                        body.pop(field)
                    else:
                        body[field] = value
                    response = self.client.post(
                        "/api/v1/opportunities/manual",
                        json=body,
                        headers={"X-Webhook-Secret": self.secret},
                    )
                    self.assertEqual(response.status_code, 422)

        self.upsert.assert_not_called()
        self.embed.assert_not_called()

    def test_valid_payload_persists_then_indexes_same_active_id(self):
        response = self.client.post(
            "/api/v1/opportunities/manual",
            json=self.valid_payload(),
            headers={"X-Webhook-Secret": self.secret},
        )

        self.assertEqual(response.status_code, 201)
        result = response.json()
        self.assertEqual(result["status"], "indexed")
        self.assertTrue(result["is_active"])
        self.upsert.assert_called_once()
        self.embed.assert_called_once()

        db_call = self.upsert.call_args.kwargs
        indexed_opportunity = self.embed.call_args.args[0][0]
        self.assertEqual(result["id"], db_call["opportunity_id"])
        self.assertEqual(db_call["opportunity_id"], indexed_opportunity["id"])
        self.assertTrue(db_call["is_active"])
        self.assertTrue(indexed_opportunity["is_active"])
        self.assertEqual(db_call["source"], "manual_form")
        self.assertEqual(db_call["data_en"]["country"], ["USA", "Egypt"])


if __name__ == "__main__":
    unittest.main()