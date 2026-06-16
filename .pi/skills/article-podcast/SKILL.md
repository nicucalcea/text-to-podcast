---
name: article-podcast
description: Publish an article URL into this repo's podcast RSS feed by triggering the GitHub Actions workflow in nicucalcea/text-to-podcast. Use when asked to create an episode from a URL, add an article to the feed, re-run a failed publication, or verify that a newly published episode is live.
---

# Article Podcast

Use this skill for this repository when the user wants an article turned into a podcast episode.

## What this project already has

- Repo: `nicucalcea/text-to-podcast`
- Workflow: `.github/workflows/publish.yml`
- Feed: `https://nicucalcea.github.io/text-to-podcast/feed.xml`
- Audio hosting: GitHub Releases
- Feed hosting: GitHub Pages from `gh-pages`
- Extraction: Jina Reader via environment secret `JINA` in environment `JINA`
- TTS: `edge-tts`, chunked and concatenated with `ffmpeg`
- Retention: latest 50 episodes

## Inputs

- Required: article URL
- Optional: language override `en`, `ro`, or `ru`
  - leave blank for auto-detect

## Default behavior

When asked to create an episode:

1. Trigger the workflow with the article URL.
2. Watch the run until it finishes.
3. If it fails, inspect the failed logs and report the real error.
4. If it succeeds, verify the RSS item and give the user the feed URL.

## Commands

Trigger a new episode:

```bash
gh workflow run publish.yml \
  -R nicucalcea/text-to-podcast \
  -f article_url='<URL>' \
  -f language=''
```

If the user explicitly wants a language override:

```bash
gh workflow run publish.yml \
  -R nicucalcea/text-to-podcast \
  -f article_url='<URL>' \
  -f language='en'
```

Watch the latest run:

```bash
gh run watch <run-id> -R nicucalcea/text-to-podcast --exit-status
```

Inspect failures:

```bash
gh run view <run-id> -R nicucalcea/text-to-podcast --log-failed
```

Check the Pages build if the feed is slow to update:

```bash
gh api repos/nicucalcea/text-to-podcast/pages/builds/latest
```

Verify the feed contains the new title:

```bash
python3 - <<'PY'
from urllib.request import Request, urlopen
req = Request('https://nicucalcea.github.io/text-to-podcast/feed.xml', headers={'Cache-Control': 'no-cache'})
xml = urlopen(req, timeout=30).read().decode('utf-8', 'replace')
print(xml[:2000])
PY
```

## Expected runtime

- Short/medium article: about 2-4 minutes
- Long article: about 5-10 minutes

If the workflow is still inside `Build episode`, that usually means TTS is still rendering chunks. This is normal for long articles.

## Troubleshooting

### Duplicate article

If the workflow stops early with a duplicate message, tell the user the article is already in the feed.

### Feed not updated yet

If the workflow succeeded but the feed URL still looks stale, check the latest Pages build. GitHub Pages may lag briefly behind the workflow.

### Build episode failed

Check failed logs first. Common causes:

- Jina fetch/extraction issue
- TTS network hiccup
- GitHub release upload failure
- Pages push issue

Do not guess. Read the logs and report the actual failing phase.

## Reply format

On success, reply with:

- article title
- whether auto-detect or manual language was used
- release/run status
- feed URL: `https://nicucalcea.github.io/text-to-podcast/feed.xml`

On failure, reply with:

- failing phase
- exact error summary
- whether you retried

## When not to use this skill

- The user wants code changes to the workflow itself rather than publishing an episode
- The user wants a local audio file only, without adding it to the feed
