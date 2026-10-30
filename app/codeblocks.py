"""Ders notu ve sunum gibi belgelerdeki örnek kodları, modele yazdırmadan olduğu gibi özete taşımak için ayıklar.

Küçük bir model kodu yeniden yazarken satırları atlar, sırasını değiştirir ya da "düzeltir"; sınav notunda ise kodun tek
bir karakteri bile önemlidir. Bu yüzden kod metinden önce ayıklanır ve numaralanır: model yalnızca "[KOD-3]" gibi bir işaret
yazar, işaretin yerine gerçek kod en son [restore] ile konur. Model kodu görür (neyi gösterdiğini anlatabilsin diye) ama
çıktıda üretmez; bu hem kodu bozulmaz kılar hem de üretilecek token sayısını azaltarak özeti hızlandırır.

Kod tanıma sezgiseldir (süslü parantezli diller, Python) ve PDF'ten çıkan metne göre ayarlıdır: sütunlara bölünmüş
satırlar birleştirilir, kaybolmuş girintiler kurulur ve yazı tipinden gelen bozuk karakterler (`i.+`, `./`, `/=`) düzeltilir.
Kodun ekrandaki çıktısı da (kodun altında ya da üstünde duruyorsa) kodun sonuna yorum satırları olarak eklenir."""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field

MARK = "[KOD-{n}]"
END_MARK = "[/KOD-{n}]"
_OPEN = re.compile(r"\[KOD-(\d+)\]")
_REF = re.compile(r"`?\[KOD-(\d+)\]`?")
_MARK_LINE = re.compile(r"^\s*(?:(?:[-*•]|\d{1,3}[.)])\s*)?`?\[KOD-(\d+)\]`?\s*[.:]?\s*$")
# Parça sınırlarında bir kodun ortadan bölünmemesi için ayıklanmış metinde bir kodun satırları bu karakterle birleşir.
JOINER = "\x1f"

_STATEMENT_START = re.compile(r"^(?:if|for|while|switch|catch)\s*\(|^(?:\}\s*)?else\b")
_HEADER = re.compile(r"^(?:(?:\}\s*)?else\s+if\s*\(.*\)|(?:if|for|while)\s*\(.*\)|(?:\}\s*)?else)$")
_DECLARATION = re.compile(
    r"^(?:(?:public|private|protected|static|final|abstract)\s+)+[\w<>\[\],.? ]+[({=;]|^(?:class|interface|enum)\s+\w+"
    r"|^(?:def|class)\s+\w+.*:\s*$|^import\s+[\w.*]+;?\s*$|^#include\b|^using\s+[\w.]+;|^package\s+[\w.]+;"
)
_WEAK = re.compile(r"^(?:[{}]+\s*;?|\)\s*\{?|\.{2,}|(?:\}\s*)?else|do\s*\{?|try\s*\{?|default\s*:|case\b.*:)\s*$|^\}\s*else\b")
_COMMENT = re.compile(r"^(?://|\./|/\.|/\*|\*/|#\s)")
_NUMERIC = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_TRAILING_NUMBER = re.compile(r"^(\s*[})]+\s*;?)\s+(-?\d+(?:[.,]\d+)?)$")
_FUSED = re.compile(
    r"^(?P<prose>[^\";{}=]+?[a-zçğıöşü]\.)\s+(?P<code>(?:[A-Za-z_][\w<>\[\]]*\s+)?[A-Za-z_][\w]*\s*(?:\[[^\]]*\]\s*)*=.*;)\s*$"
)
_OPERATOR_END = re.compile(r"(?:[+\-*/%=,(&|]|&&|\|\|)\s*$")
_STRING_OR_COMMENT = re.compile(r'("(?:\\.|[^"\\])*"|//.*$)')
_CAPTION = re.compile(r"^(?:konsol\s+)?(?:çıktı|çıktısı|output)\s*:?$", re.IGNORECASE)
_LITERAL = re.compile(r'"((?:\\.|[^"\\])*)"')


@dataclass
class Block:
    code: str
    output: list[str] = field(default_factory=list)

    def text(self) -> str:
        """Kod ve (varsa) çıktısı; çıktı kodun sonuna yorum satırları olarak yazılır."""
        return _with_output(self.code, self.output)


def _with_output(code: str, output: list[str]) -> str:
    if not output:
        return code
    if all(_NUMERIC.match(x.strip()) or x.strip() == "..." for x in output):
        return code + "\n// Çıktı: " + ", ".join(x.strip() for x in output)
    return code + "\n// Çıktı:\n" + "\n".join(("// " + x.rstrip()).rstrip() for x in output)


# --- kod satırı tanıma --------------------------------------------------------------------------------------


def _scan(text: str) -> tuple[bool, int, int]:
    """[text]'i (kod satırlarının birleşimini) tarar: (sonunda açık bir metin değeri var mı, açık parantez sayısı, açık
    süslü parantez sayısı). Metin değerleri ve // yorumları sayılmaz."""
    in_string = False
    paren = brace = 0
    i = 0
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
            elif c == "\n":  # kapanmayan metin değeri satırla biter
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "/" and text[i + 1 : i + 2] == "/":
            nl = text.find("\n", i)
            if nl < 0:
                break
            i = nl
        elif c in "([":
            paren += 1
        elif c in ")]":
            paren -= 1
        elif c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        i += 1
    return in_string, paren, brace


def _code_part(s: str) -> str:
    """Satırın sondaki // yorumu olmayan kısmı (metin değerinin içindeki // dikkate alınmaz)."""
    for m in _STRING_OR_COMMENT.finditer(s):
        if m.group(0).startswith("//"):
            return s[: m.start()].rstrip()
    return s


def _is_strong(s: str) -> bool:
    """Tek başına kod olduğu anlaşılan satır."""
    s = _code_part(s.strip())
    if not s or len(s) > 220:
        return False
    if _STATEMENT_START.match(s) and ("(" in s or s.endswith("{")):
        return True
    if _DECLARATION.match(s):
        return True
    if re.search(r"\bSystem\.\s?out\b|\bprintln\s*\(", s) or re.match(r"^\$ \S", s):  # "$ java App" gibi komut satırı
        return True
    if s.endswith((";", "{")) and (re.search(r"[(=\[]", s) or re.match(r"^(?:return|break|continue|throw)\b", s)):
        return True
    return bool(re.match(r"^\}\s*\S", s) and s.endswith((";", "{")))


def _is_code_like(s: str) -> bool:
    s = s.strip()
    return bool(s) and (
        _is_strong(s) or bool(_WEAK.match(_code_part(s)) or _COMMENT.match(s)) or (s.endswith(";") and len(s) <= 80)
    )


def _map_code(line: str, fn) -> str:
    """[fn]'i satırın metin değeri ve yorum olmayan parçalarına uygular."""
    out: list[str] = []
    last = 0
    for m in _STRING_OR_COMMENT.finditer(line):
        out.append(fn(line[last : m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(line[last:]))
    return "".join(out)


def _tidy(segment: str) -> str:
    segment = re.sub(r"(?<=\w)[./]\+", "++", segment)  # yazı tipinden gelen "i.+" / "i/+"
    segment = re.sub(r"(?<=\w)[./]-(?=[;)\s]|$)", "--", segment)
    segment = re.sub(r"(?<=\w)\.\s+(?=[A-Za-z_])", ".", segment)  # "args. length"
    segment = re.sub(r"([(\[])\s+", r"\1", segment)
    segment = re.sub(r"\s+([)\]])", r"\1", segment)
    segment = re.sub(r"(?<=\S)[ \t]{2,}", " ", segment)
    return segment


def fix_glitches(line: str) -> str:
    """PDF yazı tipinden gelen bozuk karakterleri ve fazla boşlukları düzeltir: `i.+` -> `i++`, baştaki `./` -> `//`,
    koşuldaki `/=` -> `==`, `System. out` -> `System.out`, `( kosul)` -> `(kosul)`."""
    indent = line[: len(line) - len(line.lstrip())]
    body = line.strip()
    body = re.sub(r"^(?:\./|/\.)\s?", "// ", body)
    body = re.sub(r"(?<=[\s;)])(?:\./|/\.)(?=\s)", "//", body)
    body = re.sub(r"((?:if|while)\s*\([^()]*?)\s/=\s", r"\1 == ", body)
    return indent + _map_code(body, _tidy).rstrip()


def _reindent(lines: list[str]) -> list[str]:
    """Süslü parantezli kodu parantez derinliğine göre girintiler (PDF çıkarımı girintiyi sık sık yitirir). Süslü
    parantezsiz tek komutluk gövde (`if (...)` altındaki satır) bir kademe içeri alınır."""
    out: list[str] = []
    depth = 0
    extra = 0  # üstündeki başlığın gövdesi olan, süslü parantezsiz satırlar için ek girinti
    for raw in lines:
        s = raw.strip()
        closing = s.startswith("}")
        level = max(depth - (1 if closing else 0), 0) + extra
        out.append("    " * level + s)
        depth = max(depth + _scan(s)[2], 0)
        code = _code_part(s)
        if _HEADER.match(code) and not code.endswith(("{", ";")):
            extra += 1
        else:
            extra = 0
    return out


# --- metni satırlara ayırma ---------------------------------------------------------------------------------


def _normalize(text: str) -> list[str]:
    """Satırlara ayırır; "} 9" gibi koda yapışmış tek sayılık çıktıyı ve "...kullanılabilir. int[] a = {1};" gibi cümleye
    yapışmış kodu ayırır."""
    lines: list[str] = []
    for raw in text.split("\n"):
        if m := _TRAILING_NUMBER.match(raw):
            lines += [m.group(1), m.group(2)]
        elif (m := _FUSED.match(raw)) and _is_strong(m.group("code")):
            lines += [m.group("prose"), m.group("code")]
        else:
            lines.append(raw)
    return lines


def _take(lines: list[str], start: int) -> tuple[list[str], int]:
    """[start]'tan başlayan kodu alır: (satırlar, sonraki satırın indeksi). Parantezi ya da metin değeri kapanmamış ve
    bir işleçle biten satırlar (sütun genişliği yüzünden bölünmüş ifadeler) bir sonraki satırla birleştirilir."""
    out: list[str] = []
    i = start
    carried = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        s = raw.strip()
        if out:
            open_string, paren, _ = _scan("\n".join(x.strip() for x in out))
            prev = _code_part(out[-1].strip())
            unfinished = open_string or paren > 0 or (_OPERATOR_END.search(prev) and not prev.endswith(("++", "--")))
            wrapped_comment = (  # koddan sonra başlayan bir yorumun bir alt satıra taşan devamı
                re.search(r"\S.*//[^\"]*$", out[-1])
                and re.match(r"^[a-zçğıöşü][^A-ZÇĞİÖŞÜ;{}()=]*$", s)
                and len(s) <= 40
            )
            if s and carried < 3 and (unfinished or wrapped_comment):
                out[-1] = out[-1] + " " + s
                carried += 1
                i += 1
                continue
        carried = 0
        if s and _is_code_like(s):
            out.append(raw)
            i += 1
        else:
            break
    return out, i


def _literals(code: str) -> tuple[list[str], set[str]]:
    """Koddaki yazdırma ifadelerinin metin değerleri ve (kısa değerlerden oluşan şekil çıktıları için) karakter kümesi."""
    lits = [lit.replace("\\n", "") for line in code.split("\n") if "print" in line for lit in _LITERAL.findall(line)]
    chars = {c for lit in lits if len(lit) <= 2 for c in lit} - {" "}
    return [lit.strip() for lit in lits if len(lit.strip()) >= 2], chars


def _is_output(line: str, lits: list[str], chars: set[str]) -> bool:
    s = line.strip()
    if not s:
        return False
    if any(s.startswith(lit) for lit in lits):
        return True
    return bool(chars) and set(s) <= chars | {" "}


def protect(text: str) -> tuple[str, dict[int, Block]]:
    """[text]'teki kod bloklarını bulur. Döner: (kod yerine "[KOD-n] ... [/KOD-n]" konmuş metin, n -> Block). Bir kodun
    satırları [JOINER] ile birleşir (metin parçalanırken bir bütün kalsın diye); [expand] bunu satır sonuna çevirir."""
    lines = _normalize(text)
    out: list[str] = []
    blocks: dict[int, Block] = {}
    i = 0
    while i < len(lines):
        if not _is_strong(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        lead: list[str] = []
        while out and _is_code_like(out[-1]) and JOINER not in out[-1]:  # koddan hemen önceki yorum ve açılış satırları
            lead.insert(0, out.pop())
        body, end = _take(lines, i)
        raw = lead + body
        stripped = [fix_glitches(x) for x in raw]
        if any("{" in x or x.startswith("}") for x in stripped):
            code_lines = _reindent(stripped)
        else:
            code_lines = textwrap.dedent("\n".join(stripped)).split("\n")
        code = "\n".join(code_lines)

        lits, chars = _literals(code)
        before: list[str] = []
        while out and _is_output(out[-1], lits, chars) and JOINER not in out[-1]:
            before.insert(0, out.pop())
        if before and out and _CAPTION.match(out[-1].strip()):
            out.pop()
        after: list[str] = []
        j = end
        if j < len(lines) and _CAPTION.match(lines[j].strip()):
            j += 1
        while j < len(lines) and (_NUMERIC.match(lines[j].strip()) or _is_output(lines[j], lits, chars)):
            after.append(lines[j])
            j += 1
        if after and j < len(lines) and lines[j].strip() == "...":
            after.append("...")
            j += 1
        if not after:
            j = end
        n = len(blocks) + 1
        block = Block(code, [x.rstrip() for x in before + after])
        blocks[n] = block
        out.append(JOINER.join([MARK.format(n=n), *block.text().split("\n"), END_MARK.format(n=n)]))
        i = j
    return "\n".join(out), blocks


# --- model çıktısı ------------------------------------------------------------------------------------------


def expand(text: str) -> str:
    """[protect]'in birleştirdiği kod satırlarını yeniden satırlara ayırır (metin parçalandıktan sonra çağrılır)."""
    return text.replace(JOINER, "\n")


def referenced(summary: str) -> set[int]:
    return {int(n) for n in _REF.findall(summary)}


def present(chunk: str) -> list[int]:
    """Metin parçasında bulunan kodların numaraları (metindeki sırayla)."""
    return [int(n) for n in _OPEN.findall(chunk)]


def ensure_refs(summary: str, wanted: list[int]) -> str:
    """Modelin işaretini yazmadığı kodları özete ekler; hiçbir örnek sessizce düşmesin. Eksik kod, numarası kendisinden
    küçük olan en yakın kodun hemen altına (yoksa ilk başlığın altına) konur; böylece belgedeki sırada kalır."""
    lines = summary.rstrip().split("\n")
    for n in sorted(set(wanted) - referenced(summary)):
        mark = MARK.format(n=n)
        before = [
            (int(m.group(1)), idx) for idx, line in enumerate(lines) if (m := _MARK_LINE.match(line)) and int(m.group(1)) < n
        ]
        if before:
            lines.insert(max(before)[1] + 1, mark)
            continue
        heading = next((idx for idx, line in enumerate(lines) if line.lstrip().startswith("#")), None)
        lines.insert(0 if heading is None else heading + 1, mark)
    return "\n".join(lines)


def strip_marks(text: str) -> str:
    """Kod işaretlerini ve işaret satırlarını kaldırır (genel özet için kodsuz taslak üretirken)."""
    kept = [line for line in text.splitlines() if not _MARK_LINE.match(line)]
    return _REF.sub("", "\n".join(kept))


def restore(summary: str, blocks: dict[int, Block]) -> str:
    """Özetteki "[KOD-n]" işaretlerini ``` içinde gerçek kodla değiştirir. Her kod en çok bir kez yazılır."""
    done: set[int] = set()

    def render(n: int) -> str:
        block = blocks.get(n)
        if block is None or n in done:
            return ""
        done.add(n)
        return "```\n" + textwrap.dedent(block.text()).strip("\n") + "\n```"

    out: list[str] = []
    for line in summary.split("\n"):
        if m := _MARK_LINE.match(line):
            if rendered := render(int(m.group(1))):
                out.append(rendered)
        else:
            out.append(_REF.sub(lambda mm: ("\n" + r + "\n") if (r := render(int(mm.group(1)))) else "", line))
    return "\n".join(out)
