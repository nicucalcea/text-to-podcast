#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import html
import json
import os
import re
import shlex
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape as xml_escape


SUPPORTED_LANGUAGES = {"en", "ro", "ru"}
VOICE_BY_LANGUAGE = {
    "en": "en-GB-SoniaNeural",
    "ro": "ro-RO-AlinaNeural",
    "ru": "ru-RU-SvetlanaNeural",
}
TTS_CHUNK_CHARS = 3500


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower()
    return slug or "article"


def fallback_title(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    leaf = path.split("/")[-1] if path else "article"
    leaf = leaf.replace("-", " ").replace("_", " ").strip()
    return leaf.title() or "Article"


def normalize_lang_hint(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.strip().lower().replace("_", "-")
    if lowered in {"", "auto"}:
        return None
    return lowered.split("-", 1)[0]


def detect_language(text: str, override: str | None) -> str:
    override = normalize_lang_hint(override)
    if override in SUPPORTED_LANGUAGES:
        return override

    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0
        detected = normalize_lang_hint(detect(text[:5000]))
        if detected in SUPPORTED_LANGUAGES:
            return detected
    except Exception:
        pass

    return "en"


def voice_for_language(language: str) -> str:
    return VOICE_BY_LANGUAGE.get(language, VOICE_BY_LANGUAGE["en"])


def excerpt(text: str, limit: int = 420) -> str:
    flattened = re.sub(r"\s+", " ", text).strip()
    if len(flattened) <= limit:
        return flattened
    clipped = flattened[:limit].rsplit(" ", 1)[0].strip()
    return f"{clipped}…"


def html_attr(value: str) -> str:
    return xml_escape(value, {'"': "&quot;"})


def cdata(value: str) -> str:
    return value.replace("]]>", "]]]]><![CDATA[>")


def paragraphs_to_html(text: str) -> str:
    parts = [part.strip() for part in re.split(r"\n\n+", text) if part.strip()]
    return "\n".join(
        f"<p>{html.escape(part).replace(chr(10), '<br/>')}</p>" for part in parts
    )


def rfc2822(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def format_duration(total_seconds: float | int | None) -> str | None:
    if total_seconds is None:
        return None
    seconds = max(0, int(round(float(total_seconds))))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def probe_duration(audio_path: Path) -> float | None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def build_release_tag(source_url: str, now: dt.datetime) -> str:
    digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:8]
    return f"episode-{now.strftime('%Y%m%d-%H%M%S')}-{digest}"


def build_site_url(repo: str) -> str:
    owner, name = repo.split("/", 1)
    if name == f"{owner}.github.io":
        return f"https://{owner}.github.io"
    return f"https://{owner}.github.io/{name}"


def feed_title(repo: str) -> str:
    return repo.split("/", 1)[1].replace("-", " ").title()


def load_episodes(pages_dir: Path) -> list[dict[str, Any]]:
    episodes_path = pages_dir / "episodes.json"
    if not episodes_path.exists():
        return []
    data = json.loads(episodes_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    raise ValueError(f"Expected a list in {episodes_path}")


def save_episodes(pages_dir: Path, episodes: list[dict[str, Any]]) -> None:
    ensure_dir(pages_dir)
    (pages_dir / "episodes.json").write_text(
        json.dumps(episodes, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def render_feed(repo: str, episodes: list[dict[str, Any]]) -> str:
    site_url = build_site_url(repo)
    channel_title = feed_title(repo)
    feed_url = f"{site_url}/feed.xml"
    latest_build = rfc2822(episodes[0]["created_at"]) if episodes else rfc2822(utc_now().isoformat())
    feed_image = next((episode.get("image_url") for episode in episodes if episode.get("image_url")), None)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"',
        '  xmlns:atom="http://www.w3.org/2005/Atom"',
        '  xmlns:content="http://purl.org/rss/1.0/modules/content/"',
        '  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        "  <channel>",
        f"    <title>{xml_escape(channel_title)}</title>",
        f"    <link>{xml_escape(site_url)}</link>",
        "    <description>Articles turned into a podcast feed with full text included in each episode.</description>",
        "    <language>en</language>",
        "    <itunes:explicit>false</itunes:explicit>",
        f"    <lastBuildDate>{latest_build}</lastBuildDate>",
        f"    <atom:link href=\"{html_attr(feed_url)}\" rel=\"self\" type=\"application/rss+xml\" />",
    ]
    if feed_image:
        lines.append(f"    <itunes:image href=\"{html_attr(feed_image)}\" />")

    for episode in episodes:
        lines.extend(
            [
                "    <item>",
                f"      <title>{xml_escape(episode['title'])}</title>",
                f"      <link>{xml_escape(episode['source_url'])}</link>",
                f"      <guid isPermaLink=\"false\">{xml_escape(episode['release_tag'])}</guid>",
                f"      <pubDate>{rfc2822(episode['created_at'])}</pubDate>",
                f"      <enclosure url=\"{html_attr(episode['audio_url'])}\" length=\"{int(episode['audio_size'])}\" type=\"audio/mpeg\" />",
                f"      <description><![CDATA[{cdata(episode['summary_html'])}]]></description>",
                f"      <content:encoded><![CDATA[{cdata(episode['content_html'])}]]></content:encoded>",
                f"      <itunes:summary><![CDATA[{cdata(episode['summary_text'])}]]></itunes:summary>",
                "      <itunes:explicit>false</itunes:explicit>",
            ]
        )
        if episode.get("author"):
            lines.append(f"      <itunes:author>{xml_escape(episode['author'])}</itunes:author>")
        if episode.get("duration_text"):
            lines.append(f"      <itunes:duration>{xml_escape(episode['duration_text'])}</itunes:duration>")
        if episode.get("image_url"):
            lines.append(f"      <itunes:image href=\"{html_attr(episode['image_url'])}\" />")
        lines.append("    </item>")

    lines.extend(["  </channel>", "</rss>", ""])
    return "\n".join(lines)


def write_feed_artifacts(repo: str, pages_dir: Path, episodes: list[dict[str, Any]]) -> None:
    ensure_dir(pages_dir)
    save_episodes(pages_dir, episodes)
    (pages_dir / "feed.xml").write_text(render_feed(repo, episodes), encoding="utf-8")
    (pages_dir / ".nojekyll").write_text("\n", encoding="utf-8")


def write_shell_env(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={shlex.quote(value)}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def article_metadata_summary(author: str, article_date: str | None) -> str:
    bits = []
    if author:
        bits.append(f"By {html.escape(author)}")
    if article_date:
        bits.append(f"Published {html.escape(article_date)}")
    if not bits:
        return ""
    return f"<p><em>{' • '.join(bits)}</em></p>"


def source_link_html(source_url: str) -> str:
    escaped = html.escape(source_url, quote=True)
    return f'<p><a href="{escaped}">Read the original article</a></p>'


def fetch_reader_payload(source_url: str) -> str:
    print("Fetching article with Jina Reader...", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9,ro;q=0.8,ru;q=0.7",
        "x-engine": "browser",
        "x-no-cache": "true",
    }
    api_key = os.environ.get("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-proxy"] = "auto"

    request = Request(f"https://r.jina.ai/http://{source_url}", headers=headers)
    with urlopen(request, timeout=120) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def markdown_to_text(markdown: str) -> str:
    # ponytail: markdown cleanup is regex-based and intentionally shallow; if fidelity matters later, swap this for a real markdown renderer.
    text = re.sub(r"!\[[^\]]*\]\(([^)]+)\)", "", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[*-]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)
    text = text.replace("***", "").replace("**", "").replace("__", "")
    text = re.sub(r"(?<!\w)[*_](?!\s)(.+?)(?<!\s)[*_](?!\w)", r"\1", text)
    text = text.replace("_", "").replace("*", "")
    return clean_text(html.unescape(text))


def parse_reader_payload(payload: str, source_url: str) -> dict[str, Any]:
    title_match = re.search(r"^Title:\s*(.+)$", payload, flags=re.MULTILINE)
    date_match = re.search(r"^Published Time:\s*(.+)$", payload, flags=re.MULTILINE)
    author_match = re.search(r"^Author:\s*(.+)$", payload, flags=re.MULTILINE)
    image_match = re.search(r"^Image:\s*(.+)$", payload, flags=re.MULTILINE)
    marker = "Markdown Content:\n"
    if marker not in payload:
        raise SystemExit("Jina Reader did not return article markdown")
    markdown = payload.split(marker, 1)[1].strip()
    image = clean_text(image_match.group(1) if image_match else "") or None
    if not image:
        inline_image = re.search(r"!\[[^\]]*\]\((https?://[^)]+)\)", markdown)
        image = inline_image.group(1) if inline_image else None
    text = markdown_to_text(markdown)
    return {
        "title": clean_text(title_match.group(1) if title_match else "") or fallback_title(source_url),
        "author": clean_text(author_match.group(1) if author_match else ""),
        "date": clean_text(date_match.group(1) if date_match else "") or None,
        "image": image,
        "text": text,
    }


def split_long_piece(piece: str, max_chars: int) -> list[str]:
    sentence_parts = re.split(r"(?<=[.!?])\s+", piece)
    chunks: list[str] = []
    current = ""
    for sentence in sentence_parts:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for index in range(0, len(sentence), max_chars):
                chunks.append(sentence[index:index + max_chars].strip())
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks or [piece[:max_chars].strip()]


def split_text_for_tts(text: str, max_chars: int = TTS_CHUNK_CHARS) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\n+", text) if part.strip()]
    if not paragraphs:
        return [text[:max_chars]]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(split_long_piece(paragraph, max_chars))
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


async def synthesize_chunk(text: str, voice: str, output_path: Path) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


def concat_audio(parts: list[Path], output_path: Path) -> None:
    concat_file = output_path.parent / "concat.txt"
    concat_file.write_text(
        "".join(f"file {shlex.quote(str(part.resolve()))}\n" for part in parts),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "ffmpeg failed while concatenating audio")


def synthesize_audio(text: str, voice: str, output_path: Path) -> None:
    chunks = split_text_for_tts(text)
    print(f"Synthesizing audio with {len(chunks)} chunk(s)...", flush=True)
    if len(chunks) == 1:
        asyncio.run(synthesize_chunk(chunks[0], voice, output_path))
        return

    part_dir = output_path.parent / "tts-parts"
    ensure_dir(part_dir)
    part_paths: list[Path] = []
    for index, chunk in enumerate(chunks, start=1):
        part_path = part_dir / f"part-{index:03d}.mp3"
        print(f"  chunk {index}/{len(chunks)}", flush=True)
        asyncio.run(synthesize_chunk(chunk, voice, part_path))
        part_paths.append(part_path)
    concat_audio(part_paths, output_path)


def fetch_article(source_url: str) -> dict[str, Any]:
    payload = fetch_reader_payload(source_url)
    article = parse_reader_payload(payload, source_url)
    if len(article["text"]) < 200:
        raise SystemExit("Extracted article text is too short to turn into an episode")
    return article


def build_episode(args: argparse.Namespace) -> int:
    source_url = canonicalize_url(args.url)
    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)

    article = fetch_article(source_url)
    text = article["text"]
    language = detect_language(text, args.language)
    voice = voice_for_language(language)
    title = article["title"]
    author = article["author"]
    article_date = article["date"]
    image_url = article["image"]

    print(f"Title: {title}", flush=True)
    print(f"Language: {language}", flush=True)
    print(f"Characters: {len(text)}", flush=True)

    now = utc_now()
    release_tag = build_release_tag(source_url, now)
    slug = slugify(title)[:80]
    audio_filename = f"{release_tag}-{slug}.mp3"
    audio_path = out_dir / audio_filename

    synthesize_audio(text, voice, audio_path)

    summary_text = excerpt(text)
    summary_html = "\n".join([f"<p>{html.escape(summary_text)}</p>", source_link_html(source_url)])
    content_html = "\n".join(
        [
            source_link_html(source_url),
            article_metadata_summary(author, article_date),
            paragraphs_to_html(text),
        ]
    )
    duration_seconds = probe_duration(audio_path)

    episode = {
        "title": title,
        "author": author,
        "article_date": article_date,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "source_url": source_url,
        "language": language,
        "voice": voice,
        "image_url": image_url,
        "release_tag": release_tag,
        "audio_filename": audio_filename,
        "audio_path": str(audio_path),
        "audio_size": audio_path.stat().st_size,
        "duration_seconds": duration_seconds,
        "duration_text": format_duration(duration_seconds),
        "summary_text": summary_text,
        "summary_html": summary_html,
        "content_html": content_html,
    }

    episode_json = out_dir / "episode.json"
    episode_json.write_text(json.dumps(episode, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_shell_env(
        out_dir / "episode.env",
        {
            "EPISODE_TITLE": title,
            "EPISODE_RELEASE_TAG": release_tag,
            "EPISODE_AUDIO_PATH": str(audio_path),
            "EPISODE_AUDIO_FILENAME": audio_filename,
            "EPISODE_SOURCE_URL": source_url,
        },
    )
    print(str(episode_json), flush=True)
    return 0


def episode_exists(args: argparse.Namespace) -> int:
    source_url = canonicalize_url(args.url)
    pages_dir = Path(args.pages_dir)
    episodes = load_episodes(pages_dir)
    for episode in episodes:
        if canonicalize_url(episode["source_url"]) == source_url:
            print(episode["release_tag"])
            return 0
    return 1


def update_feed(args: argparse.Namespace) -> int:
    pages_dir = Path(args.pages_dir)
    episode_path = Path(args.episode_json)
    prune_file = Path(args.prune_file)
    keep_latest = int(args.keep_latest)
    repo = args.repo

    episode = json.loads(episode_path.read_text(encoding="utf-8"))
    episode["audio_url"] = args.audio_url
    episode["release_tag"] = args.release_tag or episode["release_tag"]
    episode["audio_size"] = int(episode["audio_size"])

    episodes = [
        existing
        for existing in load_episodes(pages_dir)
        if canonicalize_url(existing["source_url"]) != canonicalize_url(episode["source_url"])
    ]
    episodes.insert(0, episode)

    removed = episodes[keep_latest:]
    kept = episodes[:keep_latest]
    write_feed_artifacts(repo, pages_dir, kept)

    prune_tags = [item["release_tag"] for item in removed if item.get("release_tag")]
    prune_file.write_text("\n".join(prune_tags) + ("\n" if prune_tags else ""), encoding="utf-8")
    print(str(pages_dir / "feed.xml"))
    return 0


def selfcheck(_: argparse.Namespace) -> int:
    import tempfile

    repo = "someone/text-to-podcast"
    sample_old = {
        "title": "Older",
        "author": "",
        "article_date": None,
        "created_at": "2026-06-15T10:00:00Z",
        "source_url": "https://example.com/older",
        "language": "en",
        "voice": VOICE_BY_LANGUAGE["en"],
        "image_url": None,
        "release_tag": "episode-old",
        "audio_filename": "old.mp3",
        "audio_path": "build/old.mp3",
        "audio_url": "https://github.com/acme/repo/releases/download/episode-old/old.mp3",
        "audio_size": 123,
        "duration_seconds": 61,
        "duration_text": "01:01",
        "summary_text": "Older summary",
        "summary_html": "<p>Older summary</p>",
        "content_html": "<p>Older body</p>",
    }
    sample_new = {
        **sample_old,
        "title": "Newest",
        "created_at": "2026-06-16T10:00:00Z",
        "source_url": "https://example.com/newest",
        "release_tag": "episode-new",
        "audio_filename": "new.mp3",
        "audio_path": "build/new.mp3",
        "audio_url": "https://github.com/acme/repo/releases/download/episode-new/new.mp3",
        "summary_text": "Newest summary",
        "summary_html": "<p>Newest summary</p>",
        "content_html": "<p>Newest body</p>",
    }

    with tempfile.TemporaryDirectory() as tmp:
        pages_dir = Path(tmp) / "gh-pages"
        save_episodes(pages_dir, [sample_old])
        episode_json = Path(tmp) / "episode.json"
        episode_json.write_text(json.dumps(sample_new), encoding="utf-8")
        prune_file = Path(tmp) / "prune.txt"
        args = argparse.Namespace(
            pages_dir=str(pages_dir),
            episode_json=str(episode_json),
            prune_file=str(prune_file),
            keep_latest=1,
            repo=repo,
            audio_url=sample_new["audio_url"],
            release_tag=sample_new["release_tag"],
        )
        update_feed(args)
        episodes = load_episodes(pages_dir)
        assert [item["release_tag"] for item in episodes] == ["episode-new"]
        assert prune_file.read_text(encoding="utf-8").strip() == "episode-old"
        feed = (pages_dir / "feed.xml").read_text(encoding="utf-8")
        assert "Newest" in feed
        assert "Older" not in feed
        assert split_text_for_tts("A\n\nB\n\nC", max_chars=3) == ["A", "B", "C"]

    print("selfcheck ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Turn an article URL into a podcast episode feed item.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_cmd = subparsers.add_parser("build", help="Extract an article and generate audio + metadata.")
    build_cmd.add_argument("--url", required=True)
    build_cmd.add_argument("--language", default="", help="Optional override: en, ro or ru. Leave blank for auto-detect.")
    build_cmd.add_argument("--out-dir", default="build")
    build_cmd.set_defaults(func=build_episode)

    exists_cmd = subparsers.add_parser("exists", help="Exit 0 if the article URL already exists in episodes.json.")
    exists_cmd.add_argument("--url", required=True)
    exists_cmd.add_argument("--pages-dir", required=True)
    exists_cmd.set_defaults(func=episode_exists)

    feed_cmd = subparsers.add_parser("update-feed", help="Merge a built episode into episodes.json and feed.xml.")
    feed_cmd.add_argument("--repo", required=True)
    feed_cmd.add_argument("--pages-dir", required=True)
    feed_cmd.add_argument("--episode-json", required=True)
    feed_cmd.add_argument("--audio-url", required=True)
    feed_cmd.add_argument("--prune-file", required=True)
    feed_cmd.add_argument("--keep-latest", default="50")
    feed_cmd.add_argument("--release-tag", default="")
    feed_cmd.set_defaults(func=update_feed)

    selfcheck_cmd = subparsers.add_parser("selfcheck", help="Run a tiny regression check for feed trimming.")
    selfcheck_cmd.set_defaults(func=selfcheck)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
