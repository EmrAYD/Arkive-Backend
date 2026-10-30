from app import codeblocks as cb

# PDF'ten çıkan ders notu metni: sütun genişliğinde bölünmüş satırlar, kayıp girinti ve bozuk "++" karakterleri.
SLIDE = """while Döngüsü (II)
Örneğin doğru kodlanmış hali:
int i = 1;
while (i <= 100) {
System.out.println("fibonacci(" +
i + "): " + fibonacci(i));
i.+;
}
i i <= 100
1 true
for Döngüsü
"""


def _blocks(text):
    protected, blocks = cb.protect(text)
    return cb.expand(protected), blocks


def test_code_is_extracted_joined_reindented_and_repaired():
    text, blocks = _blocks(SLIDE)
    assert list(blocks) == [1]
    assert blocks[1].code == (
        'int i = 1;\nwhile (i <= 100) {\n    System.out.println("fibonacci(" + i + "): " + fibonacci(i));\n    i++;\n}'
    )
    assert "[KOD-1]" in text and "[/KOD-1]" in text
    assert "while Döngüsü (II)" in text and "for Döngüsü" in text  # başlıklar kod sayılmaz


def test_prose_with_a_semicolon_or_keywords_is_not_code():
    prose = "while döngüsü koşul doğru olduğu sürece çalışır; for ise sayaçlıdır.\nSonuç olarak return komutu metodu bitirir.\n"
    assert cb.protect(prose)[1] == {}


def test_output_below_or_above_the_code_is_attached_as_comments():
    below = "int i = 1;\nSystem.out.println(i);\n1\n2\n"
    assert cb.protect(below)[1][1].text().endswith("// Çıktı: 1, 2")
    above = 'KONSOL ÇIKTISI\nDöngü\nDöngü\nfor (int i = 0; i < 2; i++) {\nSystem.out.println("Döngü");\n}\n'
    block = cb.protect(above)[1][1]
    assert block.output == ["Döngü", "Döngü"] and block.text().count("// Döngü") == 2


def test_glitches_from_the_pdf_font_are_repaired():
    assert cb.fix_glitches("i.+;") == "i++;"
    assert cb.fix_glitches("for (int i = 0; i < 9; i/+) {") == "for (int i = 0; i < 9; i++) {"
    assert cb.fix_glitches("./ Örnek") == "// Örnek"
    assert cb.fix_glitches("if (i /= 50) {") == "if (i == 50) {"
    assert cb.fix_glitches("x /= 2;") == "x /= 2;"                       # gerçek bölme-atama dokunulmaz
    assert cb.fix_glitches('System. out.println("a.  b");') == 'System.out.println("a.  b");'  # metin değeri korunur


def test_single_statement_bodies_are_indented_one_level():
    code = "if (sayi < 0)\nreturn sayi * -1;\nelse\nreturn sayi;"
    assert cb._reindent(code.split("\n")) == ["if (sayi < 0)", "    return sayi * -1;", "else", "    return sayi;"]


def test_code_fused_to_a_sentence_is_split():
    text, blocks = _blocks("clone() metodu kullanılabilir. int[] kopya = dizi.clone();\n")
    assert blocks[1].code == "int[] kopya = dizi.clone();"
    assert text.startswith("clone() metodu kullanılabilir.")


def test_summary_markers_are_replaced_with_the_exact_code_once():
    _, blocks = cb.protect("int a = 1;\nSystem.out.println(a);\n")
    out = cb.restore("## Konu\n- Örnek:\n- [KOD-1]\nSon [KOD-1] satır `[KOD-9]`", blocks)
    assert out.count("```") == 2 and "System.out.println(a);" in out   # ikinci işaret kodu yeniden basmaz
    assert "[KOD" not in out.replace("[KOD-9]", "")                   # bilinmeyen işaret sessizce silinir
    assert "- [KOD-1]" not in out                                     # madde işareti işaretle birlikte gider


def test_missing_markers_are_added_next_to_their_neighbours():
    assert cb.ensure_refs("## A\n- x\n[KOD-2]\n- y\n[KOD-5]", [1, 2, 3, 5]) == "## A\n[KOD-1]\n- x\n[KOD-2]\n[KOD-3]\n- y\n[KOD-5]"


def test_a_code_block_is_never_split_between_chunks():
    from app import summarize

    text = ("Konu cümlesi. " * 30 + "\n") * 30 + "\n".join(["int a = 1;", "int b = 2;", "System.out.println(a + b);"]) + "\n"
    protected, _ = cb.protect(text)
    parts = [cb.expand(p) for p in summarize._chunks(protected, 2000)]
    assert sum(p.count("[KOD-1]") for p in parts) == 1
    assert sum(p.count("[/KOD-1]") for p in parts) == 1
    assert any("[KOD-1]" in p and "[/KOD-1]" in p for p in parts)
