"""Özet metnini PDF'e çevirir ("AI Sonuçlarım" sayfasındaki özet dosyası).

Şablonlar [TEMPLATES] içindedir; yenisi eklemek için oraya bir [Template] eklemek yeter (uygulamadaki seçim listesi
`SummaryTemplate.kt`'de ayrıca tutulur; kimlikler aynı olmalıdır). Yazı tipleri `app/fonts` altındaki Liberation
Serif/Sans'tır (kod blokları için Liberation Mono; hepsi SIL OFL): Times New Roman ile aynı ölçülere sahip serbest karşılığıdır ve Türkçe harflerin hepsini içerir.
(PDF'in hazır "Times-Roman" yazı tipinde ğ, ş, ı, İ gibi harfler yoktur.)
"""
import io
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import Color, HexColor, black
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, XPreformatted

FONT_DIR = Path(__file__).parent / "fonts"

DEFAULT_TEMPLATE = "klasik"


@dataclass(frozen=True)
class Template:
    id: str
    family: str  # "serif" | "sans"
    body_size: float
    heading_size: float
    title_size: float
    leading: float  # satır aralığı: yazı boyutunun katı
    align: int
    title_align: int
    margins_cm: tuple[float, float, float, float]  # sol, sağ, üst, alt
    accent: str | None  # başlık rengi (None: siyah)
    title_rule: bool  # başlığın altında çizgi


TEMPLATES: dict[str, Template] = {
    t.id: t
    for t in (
        Template(
            "klasik",
            "serif", 12, 14, 14, 1.25, TA_LEFT, TA_LEFT, (2.5, 2.5, 2.5, 2.5), None, True,
        ),
        Template(
            "akademik",
            "serif", 12, 14, 14, 1.5, TA_JUSTIFY, TA_CENTER, (3.0, 2.5, 2.5, 2.5), None, False,
        ),
        Template(
            "modern",
            "sans", 11, 14, 20, 1.35, TA_LEFT, TA_LEFT, (2.2, 2.2, 2.2, 2.2), "#1E40AF", True,
        ),
        Template(
            "kompakt",
            "serif", 10.5, 13, 13, 1.15, TA_LEFT, TA_LEFT, (1.8, 1.8, 1.8, 1.8), None, False,
        ),
    )
}


def template_for(template_id: str | None) -> Template:
    """Kimlikten şablon; bilinmeyen/boş kimlik varsayılana düşer (uygulama ile sunucu sürümü farklı olabilir)."""
    return TEMPLATES.get(template_id or "", TEMPLATES[DEFAULT_TEMPLATE])


# --- Yazı tipleri ------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _register_fonts() -> None:
    """Yazı tiplerini ReportLab'e bir kez tanıtır. Özette yalnızca normal ve kalın kullanılır (italik yok); bu yüzden
    yalnızca bu ikisi tanıtılır, aile kaydında italik karşılığı olarak normal/kalın yazı verilir."""
    for name, file in {
        "LibSerif": "LiberationSerif-Regular.ttf",
        "LibSerif-Bold": "LiberationSerif-Bold.ttf",
        "LibSans": "LiberationSans-Regular.ttf",
        "LibSans-Bold": "LiberationSans-Bold.ttf",
        "LibMono": "LiberationMono-Regular.ttf",
    }.items():
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / file)))
    for family in ("LibSerif", "LibSans"):
        pdfmetrics.registerFontFamily(
            family, normal=family, bold=family + "-Bold", italic=family, boldItalic=family + "-Bold"
        )


# --- Özet metnini bloklara ayırma --------------------------------------------------------------------------

_HEADING_HASH = re.compile(r"^#{1,6}\s*(.+?)\s*#*$")
_HEADING_BOLD = re.compile(r"^\*\*(.+?)\*\*:?$")
_SUMMARY_LABEL = re.compile(r"^(özet)\s*:\s*(.+)$", re.IGNORECASE)
_BULLET = re.compile(r"^[-*•]\s+(.+)$")
_NUMBERED = re.compile(r"^(\d{1,3})[.)]\s+(.+)$")


def parse_blocks(summary: str) -> list[tuple[str, str, str]]:
    """Özet metnini (tür, metin, madde işareti) bloklarına ayırır. Tür: 'heading' | 'bullet' | 'paragraph' | 'code'.
    ``` işaretleri arasındaki satırlar, girinti ve satır sonları korunarak tek 'code' bloğu olur.

    Hem uygulamanın telefonda ürettiği biçimi ("Özet: ...", "Önemli noktalar:", "- madde") hem de bulut modelinin
    yazdığı Markdown benzeri metni (# başlık, **kalın başlık**, 1. madde) tanır."""
    blocks: list[tuple[str, str, str]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(("paragraph", " ".join(paragraph), ""))
            paragraph.clear()

    code: list[str] | None = None  # içinde olduğumuz kod bloğunun satırları

    for raw in summary.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if code is not None:
            if line.startswith("```"):
                blocks.append(("code", "\n".join(code).strip("\n"), ""))
                code = None
            else:
                code.append(raw.rstrip())
            continue
        if line.startswith("```"):
            flush()
            code = []
            continue
        if not line:
            flush()
            continue
        if m := _HEADING_HASH.match(line):
            flush()
            blocks.append(("heading", m.group(1), ""))
        elif m := _HEADING_BOLD.match(line):
            flush()
            blocks.append(("heading", m.group(1).rstrip(":"), ""))
        elif m := _SUMMARY_LABEL.match(line):
            flush()
            blocks.append(("heading", "Özet", ""))
            paragraph.append(m.group(2))
        elif m := _BULLET.match(line):
            flush()
            blocks.append(("bullet", m.group(1), "•"))
        elif m := _NUMBERED.match(line):
            flush()
            blocks.append(("bullet", m.group(2), m.group(1) + "."))
        elif line.endswith(":") and len(line) <= 60 and "." not in line[:-1]:
            flush()
            blocks.append(("heading", line.rstrip(":"), ""))
        else:
            paragraph.append(line)
    flush()
    if code:  # kapanmamış kod bloğu (model yarıda kesildi): olduğu kadarı yazılır
        blocks.append(("code", "\n".join(code).strip("\n"), ""))
    return blocks


def _wrap_code(code: str, max_chars: int) -> str:
    """Sayfaya sığmayan kod satırlarını böler (devam satırları iki boşluk içeriden başlar); girinti korunur."""
    out: list[str] = []
    for line in code.expandtabs(4).split("\n"):
        indent = len(line) - len(line.lstrip(" "))
        step = max(max_chars - indent - 2, 20)
        out.append(line[:max_chars])
        rest = line[max_chars:]
        while rest:
            out.append(" " * indent + "  " + rest[:step])
            rest = rest[step:]
    return "\n".join(out)


_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def _plain(text: str) -> str:
    text = escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    return text.replace("*", "")


def _inline(text: str) -> str:
    """Paragraf içi biçim: XML işaretleri kaçırılır, **kalın** <b> olur, kalan * ve _ işaretleri atılır. Ters tırnak
    içindeki `kod` eş aralıklı yazı tipiyle yazılır ve içindeki işaretler (ör. `*`) olduğu gibi kalır."""
    parts = _INLINE_CODE.split(text)  # tek indisler ters tırnak içindekilerdir
    return "".join(
        f'<font name="LibMono">{escape(part)}</font>' if i % 2 else _plain(part) for i, part in enumerate(parts)
    )


# --- PDF ---------------------------------------------------------------------------------------------------


def render(source_name: str, summary: str, template_id: str | None = None) -> bytes:
    """Özetin PDF'ini üretir. [source_name]: özetlenen dosyanın adı (başlığın altında yazılır)."""
    _register_fonts()
    t = template_for(template_id)
    regular = "LibSerif" if t.family == "serif" else "LibSans"
    bold = regular + "-Bold"
    accent = HexColor(t.accent) if t.accent else black
    left, right, top, bottom = (m * cm for m in t.margins_cm)

    body = ParagraphStyle(
        "body", fontName=regular, fontSize=t.body_size, leading=t.body_size * t.leading,
        alignment=t.align, spaceAfter=t.body_size * 0.6,
    )
    heading = ParagraphStyle(
        "heading", parent=body, fontName=bold, fontSize=t.heading_size, leading=t.heading_size * 1.25,
        alignment=TA_LEFT if t.align != TA_JUSTIFY else TA_LEFT, textColor=accent,
        spaceBefore=t.heading_size * 0.7, spaceAfter=t.body_size * 0.35, keepWithNext=1,
    )
    title = ParagraphStyle(
        "title", parent=heading, fontSize=t.title_size, leading=t.title_size * 1.25, alignment=t.title_align,
        spaceBefore=0, spaceAfter=t.body_size * 0.3,
    )
    subtitle = ParagraphStyle(
        "subtitle", parent=body, alignment=t.title_align, textColor=Color(0.35, 0.35, 0.35), spaceAfter=t.body_size * 0.8,
    )
    bullet = ParagraphStyle(
        "bullet", parent=body, leftIndent=t.body_size * 1.6, bulletIndent=t.body_size * 0.4,
        bulletFontName=regular, bulletFontSize=t.body_size, spaceAfter=t.body_size * 0.35,
    )

    code_size = t.body_size * 0.8
    code_style = ParagraphStyle(
        "code", fontName="LibMono", fontSize=code_size, leading=code_size * 1.3, leftIndent=6, rightIndent=6,
        backColor=HexColor("#F1F1F1"), borderPadding=4, spaceBefore=3, spaceAfter=t.body_size * 0.9,
    )
    code_chars = int((A4[0] - left - right - 12 - 8) / (0.6 * code_size))  # Liberation Mono: karakter = 0,6 em

    story = [Paragraph("Arkive ile özetlenmiştir", title), Paragraph("Kaynak dosya: " + escape(source_name), subtitle)]
    if t.title_rule:
        story += [HRFlowable(width="100%", thickness=0.8, color=accent, spaceAfter=t.body_size * 0.6)]
    for kind, text, marker in parse_blocks(summary):
        if kind == "heading":
            story.append(Paragraph(_inline(text), heading))
        elif kind == "bullet":
            story.append(Paragraph(_inline(text), bullet, bulletText=marker))
        elif kind == "code":
            story.append(XPreformatted(escape(_wrap_code(text, code_chars)), code_style))
        else:
            story.append(Paragraph(_inline(text), body))
    story.append(Spacer(1, 1))

    def page_number(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(regular, 9)
        canvas.setFillColor(Color(0.4, 0.4, 0.4))
        canvas.drawCentredString(A4[0] / 2, bottom / 2, f"Sayfa {doc.page}")
        canvas.restoreState()

    out = io.BytesIO()
    SimpleDocTemplate(
        out, pagesize=A4, leftMargin=left, rightMargin=right, topMargin=top, bottomMargin=bottom,
        title="Arkive ile özetlenmiştir - " + source_name, author="Arkive", creator="Arkive",
    ).build(story, onFirstPage=page_number, onLaterPages=page_number)
    return out.getvalue()
