"""生成腾讯云 OCR 的确定性扫描 PDF 测试材料。

脚本只创建虚构测试数据，输出为栅格 PNG 和不含文本层的单页 PDF，方便验证图片 OCR、
扫描 PDF 按页识别、数字日期、金额和表格内容。原始业务文件不会被读取或修改。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "test-data"
PNG_PATH = OUTPUT_DIR / "tencent-cloud-ocr-test-page.png"
PDF_PATH = OUTPUT_DIR / "tencent-cloud-ocr-test-scanned.pdf"
FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
)


def main() -> None:
    """生成测试 PNG 与仅含该栅格页的 PDF。"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1240, 1754), "#f6f4ee")
    draw = ImageDraw.Draw(image)
    regular = _font(27)
    small = _font(22)
    title = _font(42)
    heading = _font(29)

    draw.rounded_rectangle((56, 52, 1184, 1702), radius=4, fill="#fffef9", outline="#b8b4aa", width=2)
    _center(draw, "西安理工大学测试材料", 118, _font(24))
    _center(draw, "关于开展2026年文件智能体测试工作的通知", 184, title)
    draw.line((110, 242, 1130, 242), fill="#333333", width=2)

    draw.text((115, 278), "各测试单位：", font=heading, fill="#202020")
    draw.text((145, 333), "为验证在线文字识别、文件检索和证据定位能力，现开展扫描材料测试。", font=regular, fill="#202020")
    _section(draw, "一、测试时间", "2026年9月15日（星期二）09:30—11:30", 404, heading, regular)
    _section(draw, "二、测试地点", "金花校区教学二楼205室", 516, heading, regular)
    draw.text((115, 628), "三、测试内容", font=heading, fill="#202020")
    for index, line in enumerate(
        (
            "1. 中文标题、段落、数字、日期和标点符号识别；",
            "2. 表格行列、金额和联系电话识别；",
            "3. 扫描PDF页码、关键词和证据引用检查。",
        )
    ):
        draw.text((145, 680 + index * 48), line, font=regular, fill="#202020")

    draw.text((115, 840), "四、测试数据", font=heading, fill="#202020")
    _table(draw, top=890, regular=regular, header=small)

    draw.text((115, 1272), "五、注意事项", font=heading, fill="#202020")
    draw.text((145, 1326), "本材料仅用于系统测试，不包含真实个人信息，不得作为业务凭证。", font=regular, fill="#202020")
    draw.text((145, 1374), "联系人：测试管理员　联系电话：029-12345678", font=regular, fill="#202020")
    draw.text((145, 1422), "电子邮箱：ocr-test@example.edu.cn", font=regular, fill="#202020")
    _right(draw, "文件智能体测试组", 1508, regular)
    _right(draw, "2026年8月29日", 1556, regular)
    _center(draw, "第1页　仅用于腾讯云OCR功能测试", 1642, small, fill="#666666")

    image.save(PNG_PATH, format="PNG", optimize=True)
    # Pillow PDF 仅嵌入整页栅格图，不创建可直接提取的文字对象。
    image.save(PDF_PATH, format="PDF", resolution=150.0, quality=92)


def _font(size: int) -> ImageFont.FreeTypeFont:
    """从常见部署字体中选择支持中文的字体。"""

    for path in FONT_CANDIDATES:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    raise RuntimeError("未找到可用于生成 OCR 测试材料的中文字体。")


def _center(
    draw: ImageDraw.ImageDraw,
    text: str,
    y: int,
    font: ImageFont.FreeTypeFont,
    *,
    fill: str = "#202020",
) -> None:
    """在页面中水平居中文字。"""

    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((1240 - (box[2] - box[0])) / 2, y), text, font=font, fill=fill)


def _right(draw: ImageDraw.ImageDraw, text: str, y: int, font: ImageFont.FreeTypeFont) -> None:
    """在落款区域右侧居中文字。"""

    box = draw.textbbox((0, 0), text, font=font)
    draw.text((1090 - (box[2] - box[0]), y), text, font=font, fill="#202020")


def _section(
    draw: ImageDraw.ImageDraw,
    title: str,
    body: str,
    top: int,
    heading: ImageFont.FreeTypeFont,
    regular: ImageFont.FreeTypeFont,
) -> None:
    """绘制一组章节标题和正文。"""

    draw.text((115, top), title, font=heading, fill="#202020")
    draw.text((145, top + 52), body, font=regular, fill="#202020")


def _table(
    draw: ImageDraw.ImageDraw,
    *,
    top: int,
    regular: ImageFont.FreeTypeFont,
    header: ImageFont.FreeTypeFont,
) -> None:
    """绘制包含金额和中英文混排的四列表格。"""

    left, right = 115, 1125
    columns = (115, 225, 530, 820, 1125)
    row_height = 82
    for row in range(5):
        y = top + row * row_height
        draw.line((left, y, right, y), fill="#444444", width=2)
    for x in columns:
        draw.line((x, top, x, top + row_height * 4), fill="#444444", width=2)
    rows = (
        ("序号", "项目名称", "负责人", "预算金额"),
        ("1", "中文OCR验证", "测试员甲", "12,345.67元"),
        ("2", "扫描PDF验证", "测试员乙", "8,900.00元"),
        ("3", "证据引用验证", "测试员丙", "1,234.50元"),
    )
    for row_index, row in enumerate(rows):
        font = header if row_index == 0 else regular
        for column_index, value in enumerate(row):
            x1, x2 = columns[column_index], columns[column_index + 1]
            box = draw.textbbox((0, 0), value, font=font)
            width = box[2] - box[0]
            height = box[3] - box[1]
            x = x1 + (x2 - x1 - width) / 2
            y = top + row_index * row_height + (row_height - height) / 2 - box[1]
            draw.text((x, y), value, font=font, fill="#202020")


if __name__ == "__main__":
    main()
