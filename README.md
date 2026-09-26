# whatcanido

Publishing pipeline for "Everything you can do to help win an election".

The source of truth is a public Google Doc. `publish.py` downloads it, splits it into
pages, and writes a static site into `public/`, which Firebase Hosting serves
(`whatcanido-election`).

`public/` is generated and **not** committed. GitHub Actions rebuilds it from the Doc
and deploys:

* push to `main` → build and deploy to the live channel, so template and code changes
  publish themselves;
* **Run workflow** on the "Deploy to Firebase Hosting on merge" action → same thing with
  no commit, which is how to publish a Doc edit on its own;
* pull request → build and deploy to a preview channel, so a template change can be
  looked at before merging.

Note that a code-only push publishes the Doc as it reads at that moment: the two inputs
aren't pinned to each other. If a build fails, the deploy step doesn't run and the
previous release stays up.

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

Preview locally with `python3 -m http.server -d public 8000`. `--deploy` goes straight
to the live channel from your machine, bypassing CI.

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

## Footnotes and comments

Doc comments are stripped, inline refs and text both. Footnotes are kept: each one is
moved to the bottom of the page that cites it, under a "Notes" heading, and its
reference becomes a superscript link whose `title` shows the note text on hover.
Numbers are not reassigned — they stay as they are in the Doc, so the first note on a
page is usually not 1. A footnote cited from two pages appears on both. A reference with
no text, or a note nothing refers to, is reported as a warning.

## Why HTML and not Markdown

Google Docs' Markdown export drops heading anchor ids while still exporting internal
links as `#heading=h.xxxx`, which makes those links impossible to resolve. The HTML
export keeps both halves. It does include comments (inline `[a]` references plus a
block at the end) and footnotes — `publish.py` strips the former and relocates the
latter. Suggested edits are not exported.

## Chrome and hand-written pages

`site/template.html` and `site/style.css` hold the page shell.

Pages that aren't in the Doc live in `site/` as body fragments and are listed in
`STATIC_PAGES` in `publish.py` — currently `site/about.html` and `site/404.html`. Each
fragment gets the `<h1>` and `<title>` named there, and is itself run through template
processing, so it can use `{{doc_url}}`.
