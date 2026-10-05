from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml


TOPIC_RE = re.compile(r"/t/(?:[^/]+/)?(?P<id>\d+)")
NEXT_PAGE_RE = re.compile(r"\[(?:Next page|下一页)\]\([^)]*[?&]page=(\d+)[^)]*\)", re.IGNORECASE)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def canonical_topic(url: str) -> tuple[str, str] | None:
    match = TOPIC_RE.search(urlsplit(url).path)
    if not match:
        return None
    topic_id = match.group("id")
    parts = urlsplit(url)
    canonical = urlunsplit((parts.scheme, parts.netloc, f"/t/topic/{topic_id}", "", ""))
    return topic_id, canonical


class Archiver:
    def __init__(self, config: dict, dry_run: bool = False, max_topics: int | None = None, seed: int | None = None, only_ids: set[str] | None = None) -> None:
        self.config = config
        self.output_dir = Path(config.get("output_dir", "data"))
        self.dry_run = dry_run
        self.max_topics = max_topics if max_topics is not None else int(config.get("max_topics", 5))
        self.seed = seed if seed is not None else 0
        self.random = random.Random(seed)
        self.failures: list[dict] = []
        self.concurrency = max(1, int(config.get("concurrency", 1)))
        self.only_ids = only_ids
        self._lock = threading.Lock()
        self._counter = 0
        if not dry_run:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        self.delay_min = float(config.get("request_delay_seconds", {}).get("min", 1))
        self.delay_max = float(config.get("request_delay_seconds", {}).get("max", 3))
        self.max_retries = int(config.get("max_retries", 5))
        self.client = httpx.Client(
            timeout=float(config.get("timeout_seconds", 30)),
            follow_redirects=True,
            headers={"User-Agent": config.get("user_agent", "NodeLocPublicArchiver/0.1")},
        )

    def close(self) -> None:
        self.client.close()

    def get(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            if attempt:
                time.sleep(min(60, 2**attempt + self.random.random()))
            try:
                response = self.client.get(url)
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            retry_at = parsedate_to_datetime(retry_after)
                            delay = max(0, (retry_at - datetime.now(retry_at.tzinfo)).total_seconds())
                        time.sleep(min(300, delay))
                    response.raise_for_status()
                if response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                time.sleep(self.random.uniform(self.delay_min, self.delay_max))
                return response
            except httpx.HTTPError as exc:
                last_error = exc
        raise RuntimeError(f"Failed to fetch {url}: {last_error}")

    def sitemap_locations(self, url: str, source: str | None = None) -> list:
        root = ET.fromstring(self.get(url).content)
        namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        sitemap_urls = [node.text for node in root.findall("sm:sitemap/sm:loc", namespace) if node.text]
        if sitemap_urls:
            locations: list[dict] = []
            for sitemap_url in sitemap_urls:
                locations.extend(self.sitemap_locations(sitemap_url, sitemap_url))
            return locations
        locations = []
        for node in root.findall("sm:url", namespace):
            location = node.findtext("sm:loc", namespaces=namespace)
            if location:
                locations.append({
                    "url": location,
                    "lastmod": node.findtext("sm:lastmod", namespaces=namespace),
                    "sitemap": source or url,
                })
        return locations

    def discover_topics(self) -> list:
        sitemap_url = self.config.get("sitemap_url", "https://www.nodeloc.com/sitemap.xml")
        topics: dict[str, dict] = {}
        for entry in self.sitemap_locations(sitemap_url):
            parsed = canonical_topic(entry["url"])
            if parsed:
                topic_id, canonical_url = parsed
                topics.setdefault(topic_id, {
                    "topic_id": topic_id,
                    "url": canonical_url,
                    "lastmod": entry["lastmod"],
                    "sitemap": entry["sitemap"],
                })
        groups: dict[str, list[dict]] = {}
        for topic in topics.values():
            if self.only_ids and topic["topic_id"] not in self.only_ids:
                continue
            groups.setdefault(topic["sitemap"], []).append(topic)
        for group in groups.values():
            group.sort(
                key=lambda topic: hashlib.sha256(
                    f"{self.seed}:{topic['topic_id']}".encode("ascii")
                ).digest()
            )

        discovered: list[dict] = []
        while groups:
            for sitemap in list(groups):
                group = groups[sitemap]
                if group:
                    discovered.append(group.pop())
                if not group:
                    del groups[sitemap]
                if self.max_topics > 0 and len(discovered) >= self.max_topics:
                    return discovered
        return discovered

    def archive_topic(self, topic: dict) -> dict:
        topic_id = topic["topic_id"]
        canonical_url = topic["url"]
        topic_dir = self.output_dir / "topics" / topic_id
        metadata_path = topic_dir / "metadata.json"
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            pages = metadata.get("pages", [])
            has_base = any(p.get("kind") == "topic-base" for p in pages)
            if (
                pages
                and has_base
                and "restricted" in metadata
                and metadata.get("complete") is True
                and metadata.get("lastmod") == topic.get("lastmod")
                and all(
                (topic_dir / page["file"]).exists()
                and hashlib.sha256((topic_dir / page["file"]).read_bytes()).hexdigest() == page["sha256"]
                for page in pages
                )
            ):
                metadata["status"] = "skipped"
                return metadata
        if self.dry_run:
            return {**topic, "status": "dry-run"}
        topic_dir.mkdir(parents=True, exist_ok=True)
        pages: list[dict] = []
        json_url = f"{canonical_url}.json"
        complete = False
        restricted_placeholder = False
        try:
            response = self.get(json_url)
            payload = response.json()
            if not isinstance(payload, dict) or "post_stream" not in payload:
                raise ValueError("response is not a Discourse topic JSON object")
            raw_path = topic_dir / "topic-base.json"
            atomic_write(raw_path, response.content)
            stream = payload.get("post_stream", {}).get("stream", []) or []
            posts = payload.get("post_stream", {}).get("posts", []) or []
            seen = {p.get("id") for p in posts if isinstance(p, dict) and p.get("id") is not None}
            restricted_placeholder = (
                len(posts) == 1
                and isinstance(posts[0], dict)
                and (posts[0].get("read_permission_restricted") is True or posts[0].get("id") == -1)
            )
            pages.append({
                "format": "json",
                "kind": "topic-base",
                "url": json_url,
                "file": raw_path.name,
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "bytes": len(response.content),
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "post_ids": sorted(seen),
                "post_count": len(posts),
                "stream_count": len(stream),
            })
            missing = [pid for pid in stream if pid not in seen]
            chunk_size = int(self.config.get("posts_chunk_size", 20))
            for index in range(0, len(missing), chunk_size):
                chunk = missing[index:index + chunk_size]
                query = "&".join(f"post_ids[]={pid}" for pid in chunk)
                chunk_url = f"https://www.nodeloc.com/t/{topic_id}/posts.json?{query}"
                chunk_response = self.get(chunk_url)
                chunk_payload = chunk_response.json()
                chunk_posts = chunk_payload.get("post_stream", {}).get("posts", []) if isinstance(chunk_payload, dict) else []
                chunk_path = topic_dir / f"topic-posts-{index // chunk_size + 1:04d}.json"
                atomic_write(chunk_path, chunk_response.content)
                chunk_ids = [p.get("id") for p in chunk_posts if isinstance(p, dict) and p.get("id") is not None]
                seen.update(chunk_ids)
                pages.append({
                    "format": "json",
                    "kind": "posts-chunk",
                    "url": chunk_url,
                    "file": chunk_path.name,
                    "http_status": chunk_response.status_code,
                    "content_type": chunk_response.headers.get("content-type"),
                    "bytes": len(chunk_response.content),
                    "sha256": hashlib.sha256(chunk_response.content).hexdigest(),
                    "post_ids": chunk_ids,
                    "post_count": len(chunk_posts),
                })
            # Remove legacy markdown pages superseded by JSON.
            for legacy in topic_dir.glob("page-*.md"):
                legacy.unlink()
            expected = set(stream) if stream else seen
            restricted_only = restricted_placeholder and not stream
            complete = restricted_only or (bool(expected) and seen >= expected)
        except (httpx.HTTPError, RuntimeError, ValueError, json.JSONDecodeError):
            page = 1
            while True:
                page_url = f"{canonical_url}.md?page={page}"
                response = self.get(page_url)
                body = response.text
                digest = hashlib.sha256(response.content).hexdigest()
                page_path = topic_dir / f"page-{page:04d}.md"
                atomic_write(page_path, response.content)
                pages.append({
                    "format": "markdown",
                    "page": page,
                    "url": page_url,
                    "file": page_path.name,
                    "sha256": digest,
                })
                match = NEXT_PAGE_RE.search(body)
                if not match:
                    break
                page = int(match.group(1))
            complete = False

        post_ids = sorted({pid for page in pages for pid in page.get("post_ids", []) if pid is not None})
        stream_ids = pages[0].get("post_ids", []) if pages and pages[0].get("kind") == "topic-base" else []
        base_count = pages[0].get("stream_count", 0) if pages and pages[0].get("kind") == "topic-base" else 0
        expected_count = base_count if stream_ids or base_count else 0
        metadata = {
            "topic_id": topic_id,
            "url": canonical_url,
            "sitemap": topic.get("sitemap"),
            "lastmod": topic.get("lastmod"),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "status": "archived",
            "complete": complete,
            "restricted": restricted_placeholder,
            "expected_post_count": expected_count,
            "archived_post_count": len(post_ids),
            "post_ids": post_ids,
            "pages": pages,
        }
        atomic_write_json(metadata_path, metadata)
        return metadata

    def _process_topic(self, topic: dict) -> dict | None:
        topic_id = topic["topic_id"]
        url = topic["url"]
        try:
            result = self.archive_topic(topic)
        except Exception as exc:
            with self._lock:
                self.failures.append({"topic_id": topic_id, "url": url, "error": str(exc)})
                self._report_progress()
            return None
        with self._lock:
            self._report_progress()
        return result

    def _report_progress(self) -> None:
        self._counter += 1
        if self._counter % 50 == 0:
            print(f"progress {self._counter}: failures={len(self.failures)}", flush=True)

    def run(self) -> list:
        topics = self.discover_topics()
        results: list[dict] = []
        if self.dry_run:
            for topic in topics:
                results.append(self.archive_topic(topic))
            return results
        if self.concurrency == 1:
            for topic in topics:
                result = self._process_topic(topic)
                if result is not None:
                    results.append(result)
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                for result in executor.map(self._process_topic, topics):
                    if result is not None:
                        results.append(result)
        atomic_write_json(self.output_dir / "failures.json", self.failures)
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive public NodeLoc topics discovered via its sitemap.")
    parser.add_argument("--config", type=Path, default=Path("config.yml"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-topics", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--only-ids", type=Path)
    args = parser.parse_args()
    only_ids = None
    if args.only_ids:
        only_ids = set(json.loads(args.only_ids.read_text(encoding="utf-8")))
    archiver = Archiver(
        load_config(args.config),
        dry_run=args.dry_run,
        max_topics=args.max_topics,
        seed=args.seed,
        only_ids=only_ids,
    )
    try:
        results = archiver.run()
        print(f"Finished: {len(results)} topic(s) archived.")
    finally:
        archiver.close()


if __name__ == "__main__":
    main()
