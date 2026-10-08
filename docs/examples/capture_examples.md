# How the example captures were made

The current media in [docs/examples/](README.md) (`gallery-*.jpg` and `research-page-*.png`)
come from the real outputs of one `interactive_creative` run on 2026-10-02. The run folder is
`gs://trend-trawler-deploy-ae/2026_10_02_16_07_b133/creative_output/`. Nothing is mocked: the
gallery is the agent's own HTML file, rendered offline in Chromium.

The Oct 2025 media (`tt_prs_*.gif`, `gallery_sample_prs.png`, `its_complicated.png`,
`macho_man_prs.gif`) were moved here unchanged from the old `imgs/` folder.

## Tools

- `gcloud` with read access to the bucket. The steps only download.
- [`capture_examples.py`](capture_examples.py) has two subcommands. `prep` rewrites the
  gallery's image URLs to local files and renders the PDF pages. `optimize` resizes the
  captures and compresses them. It runs with `uv run --no-project` (Pillow and pypdfium2), so
  it needs no project dependencies.
- [`capture_gallery.mjs`](capture_gallery.mjs) is the Playwright capture. It uses the
  `playwright` package from `frontend/node_modules`, so run `cd frontend && npm install`
  first.

## Steps

```bash
# 1. Download the run's outputs (read-only) to /tmp
mkdir -p /tmp/ex
gcloud storage cp -r \
  gs://trend-trawler-deploy-ae/2026_10_02_16_07_b133/creative_output /tmp/ex/

# 2. Prep. The gallery's <img> tags point at authenticated
#    storage.mtls.cloud.google.com URLs, so rewrite them to local, downscaled JPEG
#    copies (also used by the lightbox; older galleries' XL_local_ copies too). This writes
#    /tmp/ex/creative_output/local/gallery.html and renders PDF pages 1-2 to
#    /tmp/ex/raw/research-page-{1,2}.png at 2x scale.
uv run --no-project --with pypdfium2 --with pillow python \
  docs/examples/capture_examples.py prep /tmp/ex/creative_output

# 3. Capture the gallery: a 1200x900 viewport at deviceScaleFactor 2. This writes
#    gallery-overview, gallery-full-page, gallery-hover-facts (first .gallery-item
#    hovered, which reveals .hover-text) and gallery-lightbox (image clicked, wait
#    for #lightbox.visible) to /tmp/ex/raw.
#    On a bare Linux box Chromium may need libxkbcommon. See docs/screenshots/README.md:
#      cd /tmp && apt-get download libxkbcommon0 && dpkg -x libxkbcommon0*.deb /tmp/xkb
LD_LIBRARY_PATH=/tmp/xkb/usr/lib/x86_64-linux-gnu \
NODE_PATH=frontend/node_modules \
  node docs/examples/capture_gallery.mjs /tmp/ex/creative_output/local/gallery.html /tmp/ex/raw

# 4. Optimize into docs/examples/. Images are resized to at most 1200 px wide.
#    Gallery captures become JPEG (q82, progressive) and PDF pages become
#    128-colour PNG. Each file is well under 600 KB.
uv run --no-project --with pillow python \
  docs/examples/capture_examples.py optimize /tmp/ex/raw docs/examples
```

The evaluation-report excerpt in the README was trimmed by hand from
`/tmp/ex/creative_output/creative_eval_report.json`.

To refresh with a newer run, change the `gs://` folder in step 1 and update the brief and
the JSON excerpt in [README.md](README.md). If the gallery markup changes, check the
selectors in `capture_gallery.mjs` against `creative_agent/gallery_template.py` and
`creative_agent/tools.py`.
