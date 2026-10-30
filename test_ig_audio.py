import unittest
from unittest.mock import Mock

from ig_audio import catalog


class AudioCatalogTests(unittest.TestCase):
    def test_read_only_query_and_no_temporary_download_urls(self):
        request = Mock(return_value={"audio": [{"audio_id": "123", "title": "Example",
            "is_ads_eligible": True, "download_url": "temporary-preview", "unknown": "private"}]})
        result = catalog(request, "456")
        request.assert_called_once_with("GET", "ig_audio", audio_type="music", user_id="456")
        self.assertEqual(result, [{"audio_id": "123", "title": "Example", "is_ads_eligible": True}])

    def test_missing_catalog_fails_instead_of_claiming_success(self):
        with self.assertRaises(ValueError):
            catalog(Mock(return_value={"error": "denied"}), "456")


if __name__ == "__main__":
    unittest.main()
