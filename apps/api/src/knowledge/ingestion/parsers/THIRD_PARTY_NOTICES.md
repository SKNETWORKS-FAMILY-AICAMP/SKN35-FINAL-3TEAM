# 제3자 코드 고지

이 폴더의 일부 코드는 아래 오픈소스의 알고리즘·데이터를 Python 으로 옮긴 것이다. 원본 코드를 그대로 싣지는 않았다.

## kordoc — MIT License
https://github.com/chrisryugj/kordoc · Copyright (c) 2026 chrisryugj

옮긴 곳
- `pdf/clip_tables.py`, `pdf/table_parts.py` — 한컴 PDF 칸 클립 표 복원, 쪽 넘김 표 이어 붙이기
- `pdf/open_edges.py` — 열린 표 테두리 닫기 (closeOpenTableEdges)
- `pdf/line_tables.py` 의 `build_grid_greedy` — 교차점 격자 (extractCells)
- `pdf/text_fidelity.py`, `pdf/hancom_glyphs.py` — 글자 코드 없는 한컴 글리프 지문표 (known-font-glyphs.ts, pua.ts)

```
MIT License

Copyright (c) 2026 chrisryugj

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## OpenDataLoader PDF — Apache License 2.0
https://github.com/opendataloader-project/opendataloader-pdf · Copyright 2025-2026 Hancom, Inc.

kordoc 이 옮긴 선 기반 표 찾기 알고리즘(TableBorderBuilder — 선 교차점으로 격자 만들기, createMatrix)을 거쳐
`pdf/line_tables.py` 의 `build_grid_greedy` 에 쓰였다. 바뀐 점: Java → TypeScript(kordoc) → Python, 우리 선·낱말 형식에 맞춤.
라이선스 전문: http://www.apache.org/licenses/LICENSE-2.0

## rhwp — MIT License
https://github.com/edwardkim/rhwp · Copyright edwardkim

`pdf/hancom_glyphs.py` 의 한컴 사용자 영역(PUA) 글자 → 유니코드 대응표(kordoc pua.ts 경유). 라이선스 문구는 위 MIT 와 같다.
