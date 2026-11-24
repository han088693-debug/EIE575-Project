# English deck — EIE575 TABR

English edition of the 10-page proposal deck. Same structure, same numbers, same figures as the
Chinese deck one level up; only the copy is English.

## Files

| Path | What it is |
| --- | --- |
| `page-01.svg` … `page-10.svg` | English page sources (page N ↔ `../page-NN.svg`) |
| `../EIE575_TABR_deck_EN.univer` | Slide document, unit `EIE575 TABR deck (EN)` |
| `EIE575_TABR_deck_EN.pptx` | Export handed out / presented (kept beside the sources) |

Figures are shared with the Chinese deck and referenced as `../assets/fig*.png`; nothing is duplicated here.

## How it was built

1. Copy `../page-NN.svg`, replace the text nodes with the English copy, adjust a few `font-size`
   values where English runs longer (long lines drop to 11.5–12 pt).
2. Create the `.univer` file, one Slide unit, then compile pages 1–10 with `univer_compile_svg`
   (`mode: replace`; page 1 first, later pages append).
3. `univer_lint` returns no findings on all 10 pages; screenshots reviewed page by page.
4. `univer_export` → `EIE575_TABR_deck_EN.pptx` in this folder (10 slides, 4 embedded figures).

## Keeping it in sync

The Chinese deck is the source of truth for content. When a number, claim or figure changes there,
update the matching `page-NN.svg` here and recompile that page, then re-export the pptx. Do not edit
the pptx by hand — it is generated from the SVG sources.
