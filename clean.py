"""把归档的 Discourse JSON 转成干净的结构化语料。"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

BLOCK_TAGS = {
    "p", "br", "div", "li", "tr", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}
SKIP_TAGS = {"script", "style", "svg", "img", "video", "audio", "noscript", "iframe"}

LINK_RE = re.compile(r'href="([^"]+)"')
CODE_INFO_RE = re.compile(r'<pre><code class="([^"]*)">')
IMG_RE = re.compile(r'<img[^>]*src="([^"]+)"[^>]*>', re.IGNORECASE)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.images: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag == "img":
            src = dict(attrs).get("src")
            if src:
                self.images.append(src)
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        lines = [line.strip() for line in raw.split("\n")]
        lines = [line for line in lines if line]
        return "\n".join(lines)


def extract(cooked: str) -> tuple[str, list[str], list[str]]:
    parser = TextExtractor()
    try:
        parser.feed(cooked or "")
        parser.close()
    except Exception:
        return "", [], []
    return parser.text(), parser.links, parser.images


def code_blocks(cooked: str) -> list[str]:
    blocks = []
    for match in re.finditer(r"<pre>\s*<code[^>]*>(.*?)</code>\s*</pre>", cooked or "", re.DOTALL):
        body = html.unescape(match.group(1))
        body = re.sub(r"<[^>]+>", "", body)
        blocks.append(body.strip())
    return blocks


def iter_payloads(meta: dict, base: Path) -> list[dict]:
    seen: set[int] = set()
    ordered = sorted(meta.get("post_ids") or [], key=lambda x: int(x) if str(x).lstrip("-").isdigit() else 0)
    files = []
    for page in meta.get("pages") or []:
        if page.get("format") != "json":
            continue
        files.append(base / page["file"])
    payloads = []
    for path in files:
        if not path.exists():
            continue
        try:
            payloads.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    posts = []
    for payload in payloads:
        for post in (payload.get("post_stream", {}) or {}).get("posts", []) or []:
            pid = post.get("id")
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            posts.append(post)
    return posts


def build_topic(tid: str, meta: dict) -> dict | None:
    base = Path("data/topics") / tid
    posts_raw = iter_payloads(meta, base)
    if not posts_raw:
        return None
    head = posts_raw[0]
    topic = {
        "topic_id": tid,
        "url": meta.get("url") or f"https://www.nodeloc.com/t/topic/{tid}",
        "title": head_title(head),
        "tags": head.get("tags") or [],
        "category_id": head.get("category_id"),
        "created_at": head.get("created_at"),
        "last_posted_at": head.get("last_posted_at"),
        "views": head.get("views"),
        "like_count": head.get("like_count"),
        "reply_count": head.get("reply_count"),
        "posts_count": head.get("posts_count") or len(posts_raw),
        "archived_post_count": len(posts_raw),
        "complete": meta.get("complete"),
        "restricted": meta.get("restricted"),
        "posts": [],
    }
    total_chars = 0
    for post in posts_raw:
        cooked = post.get("cooked", "") or ""
        text, links, images = extract(cooked)
        total_chars += len(text)
        topic["posts"].append(
            {
                "post_id": post.get("id"),
                "post_number": post.get("post_number"),
                "username": post.get("username"),
                "created_at": post.get("created_at"),
                "updated_at": post.get("updated_at"),
                "reply_to_post_number": post.get("reply_to_post_number"),
                "like_count": post.get("like_count"),
                "reads": post.get("reads"),
                "wiki": post.get("wiki"),
                "text": text,
                "text_chars": len(text),
                "code_blocks": code_blocks(cooked),
                "links": sorted(set(links)),
                "images": sorted(set(images)),
            }
        )
    topic["text_chars"] = total_chars
    return topic


def head_title(post: dict) -> str | None:
    return post.get("topic_title") or post.get("fancy_title")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean archived NodeLoc JSON into structured corpus.")
    parser.add_argument("--out", default="data/clean")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    out_dir = Path(args.out)
    (out_dir / "topics").mkdir(parents=True, exist_ok=True)

    metas = sorted(Path("data/topics").glob("*/metadata.json"))
    if args.limit:
        metas = metas[: args.limit]

    db_path = out_dir / "corpus.db"
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    db.execute(
        """
        CREATE TABLE topics (
            topic_id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            tags TEXT,
            category_id INTEGER,
            created_at TEXT,
            last_posted_at TEXT,
            views INTEGER,
            like_count INTEGER,
            reply_count INTEGER,
            posts_count INTEGER,
            archived_post_count INTEGER,
            text_chars INTEGER,
            complete INTEGER,
            restricted INTEGER
        )
        """
    )
    db.execute(
        """
        CREATE TABLE posts (
            post_id INTEGER PRIMARY KEY,
            topic_id TEXT,
            post_number INTEGER,
            username TEXT,
            created_at TEXT,
            updated_at TEXT,
            reply_to_post_number INTEGER,
            like_count INTEGER,
            reads INTEGER,
            wiki INTEGER,
            text TEXT,
            text_chars INTEGER,
            code_blocks TEXT,
            links TEXT,
            images TEXT
        )
        """
    )

    stats = Counter()
    for meta_path in metas:
        tid = meta_path.parent.name
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            stats["meta_parse_error"] += 1
            continue
        topic = build_topic(tid, meta)
        if topic is None:
            stats["no_payload"] += 1
            continue
        if topic["restricted"]:
            stats["restricted"] += 1
        if topic["complete"] is not True:
            stats["incomplete"] += 1

        payload = {
            "topic_id": topic["topic_id"],
            "url": topic["url"],
            "title": topic["title"],
            "tags": topic["tags"],
            "category_id": topic["category_id"],
            "created_at": topic["created_at"],
            "last_posted_at": topic["last_posted_at"],
            "views": topic["views"],
            "like_count": topic["like_count"],
            "reply_count": topic["reply_count"],
            "posts_count": topic["posts_count"],
            "archived_post_count": topic["archived_post_count"],
            "text_chars": topic["text_chars"],
            "complete": topic["complete"],
            "restricted": topic["restricted"],
            "posts": topic["posts"],
        }
        tmp = out_dir / "topics" / f"{tid}.json"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        db.execute(
            "INSERT OR REPLACE INTO topics VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                topic["topic_id"], topic["title"], topic["url"],
                json.dumps(topic["tags"], ensure_ascii=False), topic["category_id"],
                topic["created_at"], topic["last_posted_at"], topic["views"],
                topic["like_count"], topic["reply_count"], topic["posts_count"],
                topic["archived_post_count"], topic["text_chars"],
                1 if topic["complete"] else 0, 1 if topic["restricted"] else 0,
            ),
        )
        for post in topic["posts"]:
            db.execute(
                "INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    post["post_id"], tid, post["post_number"], post["username"],
                    post["created_at"], post["updated_at"], post["reply_to_post_number"],
                    post["like_count"], post["reads"], 1 if post["wiki"] else 0,
                    post["text"], post["text_chars"],
                    json.dumps(post["code_blocks"], ensure_ascii=False),
                    json.dumps(post["links"], ensure_ascii=False),
                    json.dumps(post["images"], ensure_ascii=False),
                ),
            )
        stats["topics"] += 1
        stats["posts"] += len(topic["posts"])
        if stats["topics"] % 2000 == 0:
            db.commit()
            print(f"processed {stats['topics']}", flush=True)

    db.commit()
    print("=== clean summary ===")
    for key, value in stats.items():
        print(f"{key}: {value}")
    row = db.execute("SELECT COUNT(*), SUM(text_chars) FROM posts").fetchone()
    print(f"posts_in_db={row[0]} total_text_chars={row[1]}")
    row = db.execute("SELECT COUNT(*) FROM posts WHERE text IS NOT NULL AND text != ''").fetchone()
    print(f"posts_with_text={row[0]}")
    db.close()
    print(f"db={db_path}")


if __name__ == "__main__":
    main()