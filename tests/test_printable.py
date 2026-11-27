import re

from spt.boards import REFERENCE, TARGET, layout_rects
from spt.pdf import MM_TO_PT, PdfPage, write_pdf
from spt.printable import LEFT_MM, PAPER_MM, TOP_MM, board_page


def test_pdf_structure_and_offsets(tmp_path):
    path = tmp_path / "boards.pdf"
    write_pdf(path, [board_page(REFERENCE), board_page(TARGET)])
    data = path.read_bytes()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    startxref = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[startxref:].startswith(b"xref")
    offsets = [int(m) for m in re.findall(rb"(\d{10}) 00000 n", data)]
    for number, offset in enumerate(offsets, start=1):
        assert data[offset:].startswith(f"{number} 0 obj".encode())
    assert data.count(b"/Type /Page ") == 2


def test_pages_fit_on_paper():
    for paper, (w, h) in PAPER_MM.items():
        for spec in (REFERENCE, TARGET):
            bw, bh = spec.size_mm
            # board, right-hand scale bar + label, bottom scale bar + label
            assert LEFT_MM + bw + 12 + 16 < w - 5, (paper, spec.name)
            assert TOP_MM + bh + 8 + 5 < h - 5, (paper, spec.name)


def test_board_drawn_at_exact_size():
    page = board_page(REFERENCE)
    ops = page.content().decode()
    # The bottom-right black square must end exactly at the board's far corner.
    rects = layout_rects(REFERENCE)
    max_x = max(x + w for x, _, w, _ in rects)
    max_y = max(y + h for _, y, _, h in rects)
    bw, bh = REFERENCE.size_mm
    assert abs(max_x - bw) < 1e-9 and abs(max_y - bh) < 1e-9
    x_pt = f"{(LEFT_MM + bw - REFERENCE.square_mm) * MM_TO_PT:.4f}".rstrip("0").rstrip(".")
    assert x_pt in ops


def test_text_is_escaped():
    page = PdfPage(100, 100)
    page.text(10, 10, "a (b) \\ c")
    assert "(a \\(b\\) \\\\ c) Tj" in page.content().decode()
