"""Build the PU-branded edition of the TABR deck from the group template.

Run from anywhere:  python TABR_project/tools/build_pu_deck.py
The template lives outside the repo (received by chat); pass a path as argv[1]
to override the default.

Source: E:/xwechat_files/.../EIE575_TABR_Group3.pptx (14 slides, Palatino, PU header)
Output: TABR_project/docs/deck/pu/EIE575_TABR_Group3.pptx

Edits: refresh the seed wording, fold in the beta sweep (new slide 12), and update
limitations/next steps. Figures come from docs/deck/assets.
"""
import copy
import io
import os
import sys

from pptx import Presentation
from pptx.util import Emu
from PIL import Image

DEFAULT_SRC = r"E:/xwechat_files/wxid_iphuvwp1dah512_c7f9/msg/file/2026-11/EIE575_TABR_Group3.pptx"
SRC = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASSETS = os.path.join(ROOT, "TABR_project", "docs", "deck", "assets")
OUT = os.path.join(ROOT, "TABR_project", "docs", "deck", "pu", "EIE575_TABR_Group3.pptx")


def set_text(shape, text):
    """Replace a shape's text, keeping the first run's formatting."""
    tf = shape.text_frame
    p = tf.paragraphs[0]
    if not p.runs:
        p.add_run()
    p.runs[0].text = text
    for r in p.runs[1:]:
        r._r.getparent().remove(r._r)
    for extra in list(tf.paragraphs[1:]):
        extra._p.getparent().remove(extra._p)


def clone_slide(prs, src_slide):
    new = prs.slides.add_slide(prs.slide_layouts[6])
    for shp in list(new.shapes):
        shp._element.getparent().remove(shp._element)
    tree = new.shapes._spTree
    for el in src_slide.shapes._spTree:
        if el.tag.endswith("}nvGrpSpPr") or el.tag.endswith("}grpSpPr"):
            continue
        tree.append(copy.deepcopy(el))
    # remap image relationships
    for blip in tree.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip"):
        rId = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        if not rId:
            continue
        part = src_slide.part.rels[rId].target_part
        blip.set(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed",
            new.part.relate_to(part, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"),
        )
    return new


def move_slide(prs, from_idx, to_idx):
    sldIdLst = prs.slides._sldIdLst
    ids = list(sldIdLst)
    el = ids[from_idx]
    sldIdLst.remove(el)
    ids2 = list(sldIdLst)
    if to_idx >= len(ids2):
        sldIdLst.append(el)
    else:
        ids2[to_idx].addprevious(el)


def find(slide, name):
    for sh in slide.shapes:
        if sh.name == name:
            return sh
    raise KeyError(name)


def swap_picture(slide, name, path):
    """Replace a picture in place, fitting it inside the original box."""
    pic = find(slide, name)
    left, top, w, h = pic.left, pic.top, pic.width, pic.height
    pic._element.getparent().remove(pic._element)
    iw, ih = Image.open(path).size
    scale = min(w / iw, h / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    slide.shapes.add_picture(path, left + (w - nw) // 2, top + (h - nh) // 2,
                             width=Emu(nw), height=Emu(nh))


def main():
    prs = Presentation(SRC)
    S = list(prs.slides)

    # --- slide 6: seed wording -------------------------------------------
    set_text(
        find(S[5], "TextBox 25"),
        "S1 and v4 use three seeds, with policies paired on the same seed; calibration uses two. "
        "Packets, vehicle pairs, and events within one run are not independent replicates. The deadline "
        "is 0.8 s and the observation window is 2 s; timeouts and right-censored cases are counted "
        "separately, and S0 delay is N/A.",
    )

    # --- refresh every embedded figure ----------------------------------
    swap_picture(prs.slides[6], "Picture 6", os.path.join(ASSETS, "fig1_working_point.png"))
    s8 = prs.slides[7]
    swap_picture(s8, "Picture 6", os.path.join(ASSETS, "fig2_cbr_vs_rate.png"))
    swap_picture(s8, "Picture 7", os.path.join(ASSETS, "fig4_load_ladder.png"))
    swap_picture(prs.slides[8], "Picture 6", os.path.join(ASSETS, "fig3_s1_comparison.png"))
    set_text(
        find(s8, "TextBox 8"),
        "Left: CBR versus base rate by density; right: base rate and CBR versus density at target 0.18.",
    )
    set_text(
        find(s8, "TextBox 10"),
        "Four d = 33 calibration points fall on one line (0.037 per Hz); the d = 30 anchor is 0.41. At "
        "the same 10 Hz, d = 5 gives only 0.083, so CBR is set by rate and density together \u2014 lowering "
        "b buys separation but also empties the channel.",
    )

    # --- new beta slide, cloned from the calibration slide ---------------
    beta = clone_slide(prs, S[6])
    move_slide(prs, len(prs.slides) - 1, 11)  # after slide 11 (v4)
    for sh in beta.shapes:
        if sh.top is not None and sh.top > 530 * 12700 and sh.width and sh.width < 900 * 12700:
            sh.width = Emu(int(960 * 12 / 15) * 12700)

    fig5 = os.path.join(ASSETS, "fig5_beta_sweep.png")
    w, h = Image.open(fig5).size
    pic = max((sh for sh in beta.shapes if sh.shape_type == 13), key=lambda sh: sh.width)
    pic._element.getparent().remove(pic._element)
    width = Emu(648 * 12700)
    height = Emu(int(width * h / w))
    beta.shapes.add_picture(fig5, Emu(154.8 * 12700), Emu(95 * 12700), width=width, height=height)

    set_text(find(beta, "TextBox 3"), "Sensitivity to \u03b2: the Gain Saturates at \u03b2 \u2248 1")
    set_text(
        find(beta, "TextBox 7"),
        "Rate law, delay floor and clipping bound from the \u03b2 sweep (v4 compact, S1, seed 300).",
    )
    set_text(
        find(beta, "TextBox 8"),
        "The rate law holds exactly: r = b(1+\u03b2) with b = 2.31 Hz. Between \u03b2 = 0.5 and \u03b2 = 1 the "
        "post-detection wait drops from 32.2 ms to 0.5 ms, one frame, and the delay then stays at "
        "100.5 ms (100 ms detection plus one frame). Doubling \u03b2 to 2 buys nothing and costs only 21 "
        "frames in 122,000. Beyond \u03b2 = r_max/b \u2212 1 the multiplier is clipped: at b \u2248 3.07 Hz, "
        "\u03b2 = 3 saturates the whole braking window and changes no delay.",
    )

    # --- conclusions: add the beta point --------------------------------
    concl = prs.slides[12]
    set_text(
        find(concl, "TextBox 8"),
        "Under v4's fixed background load, the benefit persists: G0 delivers at 244.8 ms and G2 at "
        "116.6 ms, a 52% reduction with unchanged transmissions. The structural limitation is itself a "
        "result: distinguishability and congestion are mutually exclusive under all_vehicles_same_scheme, "
        "which explains grid_exhausted and motivates the decoupled design. The gain also saturates in "
        "\u03b2: below one the wait is still above a frame, and beyond one nothing improves.",
    )

    # --- limitations and next steps --------------------------------------
    lim = prs.slides[13]
    set_text(
        find(lim, "TextBox 6"),
        "The study covers 80 km/h, 250 B messages, one channel and one MAC priority; the 0.8 s deadline "
        "is loose, so the benefit shows as delay rather than timeout rate. The \u03b2 sweep uses one seed "
        "per level and the supplementary batches run at low load with no braking event.",
    )
    set_text(
        find(lim, "TextBox 7"),
        "Three extensions follow. First, repeat the speed-change test where b stays below r_max/(1+\u03b2): "
        "the first attempt did activate S (peak W = 1.99), but b was already at 10 Hz, so no difference "
        "could appear. Second, tighten the deadline, for example to 0.2 s, or raise the background load "
        "further to expose a difference in timeout rate. Third, turn the single-seed \u03b2 sweep into a "
        "three-seed matrix across low and high b, and add multiple channels and MAC priorities.",
    )

    # normalise the progress bar to the new slide count
    n = len(prs.slides)
    for i, sl in enumerate(prs.slides):
        for sh in sl.shapes:
            if sh.top is not None and sh.top > 530 * 12700 and sh.width and 0 < sh.width < 955 * 12700:
                sh.width = Emu(int(960 * (i + 1) / n) * 12700)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    prs.save(OUT)
    print("saved", OUT, "slides", len(prs.slides))


if __name__ == "__main__":
    main()
