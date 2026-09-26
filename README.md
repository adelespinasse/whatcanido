# whatcanido

Publishing pipeline for "Everything you can do to help win an election".

The source of truth is a public Google Doc. `publish.py` downloads it, splits it into
pages, and writes a static site into `public/`, which Firebase Hosting serves
(`whatcanido-election`). The generated files in `public/` are committed, because the
GitHub Actions workflows deploy the repo contents directly.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Building

```sh
.venv/bin/python publish.py              # download the doc, rebuild public/
.venv/bin/python publish.py --deploy     # ...and then firebase deploy --only hosting
.venv/bin/python publish.py --cache doc.html   # cache the export locally (fast iteration)
.venv/bin/python publish.py --cache doc.html --refetch   # refresh that cache
```

Preview locally with `python3 -m http.server -d public 8000`.

## Markers in the document

| Marker | Effect |
| --- | --- |
| `<<<HEADER>>>` … `<<<END HEADER>>>` | Everything between them, inclusive, is dropped from the site. Put the status/TODO notes there. |
| `<<<TOC>>>` or `[TOC]` | Replaced by the front-page table of contents. |

Each marker must be the paragraph's whole text (`[TOC]` may sit in a paragraph of its
own). If the header markers are missing the build warns and publishes everything; a
start marker with no end marker is an error.

## How the pages come out

* Everything before the first Heading 1 becomes the front page (`index.html`), titled
  from the doc's Title-styled paragraph.
* Each Heading 1 starts a new page, named from a slug of its text.
* The front page's TOC lists every other page plus its H2 and H3 headings.
* Each section page gets a TOC of its own H2–H5 headings, immediately after the H1.
* Internal doc links (`#h.xxxx`) are rewritten to the right page and anchor; anything
  unresolvable is turned into plain text and reported as a warning.

Adjust `FRONT_TOC_MAX_LEVEL`, `SECTION_TOC_LEVELS`, the markers, and `DOC_ID` at the
top of `publish.py`.

## Why HTML and not Markdown

Google Docs' Markdown export drops heading anchor ids while still exporting internal
links as `#heading=h.xxxx`, which makes those links impossible to resolve. The HTML
export keeps both halves. It does include comments (inline `[a]` references plus a
block at the end) — `publish.py` strips them. Suggested edits are not exported.

## Chrome

`site/template.html` and `site/style.css` hold the page shell. The header and footer
content is still a placeholder (marked `TBD` in the template).
