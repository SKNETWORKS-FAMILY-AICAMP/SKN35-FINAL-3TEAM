# -*- coding: utf-8 -*-
"""글자 충실도 바로잡기(text_fidelity) 단위 테스트 — 따옴표 되살리기, 원문자 합치기."""
from knowledge.ingestion.parsers.pdf.text_fidelity import FidelityLog, compose_circled, fix_words, restore_quotes


def test_quotes_restored_in_order_when_counts_match():
    qs = [("“", 10, 5, 10), ("”", 40, 5, 10)]
    assert restore_quotes('"금융" 기준', qs) == "“금융” 기준"


def test_quotes_left_alone_when_counts_differ():
    """원본 따옴표 수와 다르면 엉뚱한 자리를 바꾸지 않게 그대로 둔다."""
    assert restore_quotes("'25.9 '26.1", [("’", 10, 5, 10)]) is None


def test_fix_words_uses_quotes_inside_word_box():
    words = {1: [("'25.9", 100, 100, 130, 110), ("'26.1", 200, 100, 230, 110)]}
    quotes = {1: [("’", 101, 105, 8), ("’", 201, 105, 8)]}
    log = FidelityLog()
    out = fix_words(words, quotes, log)
    assert [w[0] for w in out[1]] == ["’25.9", "’26.1"] and log.words == 2


def test_circled_number_composed():
    assert compose_circled("전금업 매출액 (◯1 ) ③=◯1 -◯2")[0] == "전금업 매출액 (① ) ③=① -②"
    assert compose_circled("◯21")[1] == 0          # 20 넘으면 원문자가 없다


def test_hancom_pua_codes_mapped():
    """한컴 사설 코드: 네모 안 숫자 U+F02B1 → ①, 원 안 0 → ⓪, 일의 자리 숫자 → 숫자, 표 항목([예]·└)."""
    from knowledge.ingestion.parsers.pdf.text_fidelity import map_pua_code
    assert map_pua_code(0xF02B1) == "①" and map_pua_code(0xF0288) == "⓪" and map_pua_code(0xF0295) == "3"
    assert map_pua_code(0xF00DA) == "[예]" and map_pua_code(0xF080C) == "└" and map_pua_code(0xF0999) is None


def test_known_pua_needs_known_face_and_exact_print():
    """확인된 한컴 글꼴이 아니거나 지문이 다르면 되살리지 않는다 (모양 추정 없음)."""
    from knowledge.ingestion.parsers.pdf.text_fidelity import known_pua
    outline = ([[[0, 0, True], [100, 0, True], [100, 100, True]]], 1000)
    assert known_pua("ABCDEF+Arial", outline, 1000) is None
    assert known_pua("ABCDEF+HCRDotum", outline, 1000) is None


def test_restored_glyph_may_be_multi_char_or_empty():
    """되살린 글은 여러 글자([예])이거나 빈 글(두 자리 번호 뒤 조각)일 수 있다."""
    pu = chr(0xF000)
    assert restore_quotes(f"{pu} 단풍", [("[예]", 0, 0, 10)]) == "[예] 단풍"
    assert restore_quotes(f"{pu}{pu} 항목", [("⑯", 0, 0, 10), ("", 5, 0, 10)]) == "⑯ 항목"
