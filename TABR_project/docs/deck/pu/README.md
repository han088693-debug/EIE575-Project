# PU-品牌版式（PolyU 模板）

按小组统一的 PolyU 模板（`EIE575_TABR_Group3.pptx`，14 页、Palatino Linotype、红色页眉、右下角校徽与页脚进度条）重排的版本，共 15 页。内容取自 `docs/REPORT_20261124.md`，数字与报告一致。

## 与另外两版的关系

| 版本 | 位置 | 版式来源 | 生成方式 |
| --- | --- | --- | --- |
| 中文版 | `../EIE575_TABR_deck.pptx` | 自建（F5F7FA 留白风） | `../page-NN.svg` → univer → pptx |
| 英文版 | `../en/EIE575_TABR_deck_EN.pptx` | 同上 | `../en/page-NN.svg` → univer → pptx |
| **本版** | 本目录 | 小组 PolyU 模板 | 直接基于模板 pptx 改，脚本 `TABR_project/tools/build_pu_deck.py` |

本版是这份清单里唯一不经过 SVG 的版本：模板本身是带母版与校徽的 pptx，重画成 SVG 会丢失母版字体（Palatino Linotype 的衬线字形会回退）。因此以模板 pptx 为源，用脚本做定点修改，避免手改。

## 重新生成

```
python TABR_project/tools/build_pu_deck.py
# 模板换了位置就传路径：
python TABR_project/tools/build_pu_deck.py "D:/path/to/EIE575_TABR_Group3.pptx"
```

脚本做五件事，全部可重放：

1. 平台页的种子口径改为"S1 与 v4 用 3 个种子、校准 2 个"；
2. 在 v4 页之后插入 β 敏感性页（复制校准页版式，换成 `fig5_beta_sweep.png`）；
3. 结论页补 β 饱和一句；
4. 局限与下一步页改写：说明变速实验已做过一次、落在 10 Hz 天花板右侧，下一步要在 `b(1+β) < r_max` 的工作点重做；
5. 全篇进度条按新的 15 页重算宽度。

## 图片选择

| 图 | 用在哪 | 为什么留 |
| --- | --- | --- |
| `fig1_working_point.png` | 校准页 | 四个 target 的 b 与 CBR 一图说清 |
| `fig2_cbr_vs_rate.png` + `fig4_load_ladder.png` | CBR 由车队自己产生 | 左图给斜率、右图给密度扫描，缺一不可 |
| `fig3_s1_comparison.png` | S1 结果页 | 主结果的三栏对比（D_info 两种口径 + 速率） |
| `fig5_beta_sweep.png` | β 敏感性页（新） | 速率律、时延地板、钳制上限，一张图讲完 |

未用的 `fig6_v4_deployment.png` 与 `fig7_delay_split.png`：v4 页的表格已把 CBR／速率／时延／投递率四项并排列出，再放图会重复；延迟分解的结论已经写进 S1 页正文。若之后把 v4 页改成图文版，优先用 fig6。

## 改数的时候

数字以报告为准。报告改了数字，重跑一次脚本；不要直接改本目录的 pptx。
