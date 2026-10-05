import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive import Archiver, NEXT_PAGE_RE, canonical_topic


class ArchiveTests(unittest.TestCase):
    def test_canonical_topic(self):
        self.assertEqual(
            canonical_topic("https://www.nodeloc.com/t/example/123?page=2#post_3"),
            ("123", "https://www.nodeloc.com/t/topic/123"),
        )
        self.assertEqual(
            canonical_topic("https://www.nodeloc.com/t/456"),
            ("456", "https://www.nodeloc.com/t/topic/456"),
        )
        self.assertIsNone(canonical_topic("https://www.nodeloc.com/categories"))

    def test_next_page_recognizes_chinese_and_english(self):
        english = NEXT_PAGE_RE.search("[Next page](/t/topic/1.md?page=2)")
        chinese = NEXT_PAGE_RE.search("[下一页](/t/topic/1.md?page=3)")
        self.assertIsNotNone(english)
        self.assertIsNotNone(chinese)
        self.assertEqual(english.group(1), "2")
        self.assertEqual(chinese.group(1), "3")

    def test_fixed_seed_sampling_is_stable(self):
        entries = [
            {"url": f"https://www.nodeloc.com/t/topic/{topic_id}", "lastmod": None, "sitemap": sitemap}
            for sitemap in ("a", "b")
            for topic_id in range(1, 7)
        ]
        config = {"output_dir": "unused", "max_topics": 6}
        first = Archiver(config, dry_run=True, seed=42)
        second = Archiver(config, dry_run=True, seed=42)
        try:
            with patch.object(first, "sitemap_locations", return_value=entries):
                first_ids = [item["topic_id"] for item in first.discover_topics()]
            with patch.object(second, "sitemap_locations", return_value=entries):
                second_ids = [item["topic_id"] for item in second.discover_topics()]
            self.assertEqual(first_ids, second_ids)
        finally:
            first.close()
            second.close()

    def test_existing_file_hash_is_verified_before_skip(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            topic_dir = output_dir / "topics" / "123"
            topic_dir.mkdir(parents=True)
            payload = b'{"post_stream":{"posts":[],"stream":[]}}'
            (topic_dir / "topic.json").write_bytes(payload)
            (topic_dir / "metadata.json").write_text(
                "{\"lastmod\":\"same\",\"complete\":true,\"restricted\":false,\"pages\":[{\"kind\":\"topic-base\",\"file\":\"topic.json\",\"sha256\":\""
                + hashlib.sha256(payload).hexdigest()
                + "\"}]}",
                encoding="utf-8",
            )
            archiver = Archiver({"output_dir": str(output_dir)}, dry_run=False)
            try:
                result = archiver.archive_topic(
                    {"topic_id": "123", "url": "https://www.nodeloc.com/t/topic/123", "lastmod": "same"}
                )
                self.assertEqual(result["status"], "skipped")
                (topic_dir / "topic.json").write_bytes(b"changed")
                with patch.object(archiver, "get", side_effect=RuntimeError("hash mismatch triggered fetch")):
                    with self.assertRaisesRegex(RuntimeError, "hash mismatch triggered fetch"):
                        archiver.archive_topic(
                            {"topic_id": "123", "url": "https://www.nodeloc.com/t/topic/123", "lastmod": "same"}
                        )
            finally:
                archiver.close()


if __name__ == "__main__":
    unittest.main()
