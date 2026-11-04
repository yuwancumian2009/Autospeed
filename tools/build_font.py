"""按图表实际会渲染的字符串生成中文字体子集，并校验无缺字。

不用 pyftsubset CLI，是为了让"需要哪些字"由下面的 STRINGS 唯一决定，
加新标签时只要同步这个列表，就不会再出现缺字形（豆腐块）的问题。
"""
import os
from fontTools.ttLib import TTCollection, TTFont
from fontTools import subset

SRC = "/workspace/chromedeps/root/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FACE = 2  # Noto Sans CJK SC
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "static", "fonts", "NotoSansSC-Subset.ttf")

# 图表里出现的所有中文/符号文案（数字、日期、英文由 ASCII 覆盖）
STRINGS = [
    "网络测速趋势",
    "近 %d 天 · 共 %d 次测速 · 右上为最新一次",
    "近 30 天 · 共 27 次测速 · 右上为最新一次",
    "全部历史 · 共 27 次测速 · 右上为最新一次",
    "下行 Mbps", "上行 Mbps", "延迟 ms",
    "下行", "上行", "延迟", "Mbps", "ms",
    "暂无数据，等待首次测速",
    "最近 7 天 · 平均下行 92.6 · 最高 93.4",
]

TEXT = "".join(sorted({c for s in STRINGS for c in s}))
TEXT += "".join(chr(c) for c in range(0x20, 0x7F))

coll = TTCollection(SRC, lazy=False)
font = coll.fonts[FACE]
opts = subset.Options()
opts.layout_features = ["*"]
opts.hinting = False
opts.desubroutinize = True
opts.name_IDs = ["*"]
opts.notdef_outline = True

sub = subset.Subsetter(options=opts)
sub.populate(text=TEXT)
sub.subset(font)
font.save(OUT)

# 校验：STRINGS 里的每个字都必须可渲染
check = TTFont(OUT).getBestCmap()
missing = sorted({c for s in STRINGS for c in s if ord(c) not in check})
print("输出:", OUT, "%.1f KB" % (os.path.getsize(OUT) / 1024))
print("字形数:", len(check))
print("缺字:", missing if missing else "无")
raise SystemExit(1 if missing else 0)
