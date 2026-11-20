# -*- coding: utf-8 -*-
"""블록 목록 → DoclingDocument. 이후 청킹·내보내기는 Docling 쪽과 같은 형식으로 이어진다."""
from docling_core.types.doc import ContentLayer, DocItemLabel, DoclingDocument, TableCell, TableData

FURN = {"header": DocItemLabel.PAGE_HEADER, "footer": DocItemLabel.PAGE_FOOTER, "footnote": DocItemLabel.FOOTNOTE}


def build(blocks: list, name: str) -> DoclingDocument:
    doc = DoclingDocument(name=name)
    for b in blocks:
        t = b["type"]
        if t == "title":
            doc.add_title(text=b["text"])
        elif t == "heading":
            doc.add_heading(text=b["text"], level=max(1, min(100, b["level"])))
        elif t == "para":
            doc.add_text(label=DocItemLabel.TEXT, text=b["text"])
        elif t == "furniture":
            doc.add_text(label=FURN[b["kind"]], text=b["text"], content_layer=ContentLayer.FURNITURE)
        elif t == "table":
            cells = [TableCell(start_row_offset_idx=r, end_row_offset_idx=r + rs, start_col_offset_idx=c,
                               end_col_offset_idx=c + cs, row_span=rs, col_span=cs, text=txt)
                     for r, c, rs, cs, txt in b["cells"]]
            doc.add_table(data=TableData(num_rows=b["rows"], num_cols=b["cols"], table_cells=cells))
    return doc
