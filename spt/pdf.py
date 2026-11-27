"""Minimal vector PDF writer (filled rectangles and Helvetica text only).

Positions are in millimetres measured from the top-left of the page, to match the
board coordinates. No third-party dependency, and the output is exact: the
printer rasterises the squares itself at its native resolution.
"""

from __future__ import annotations

from pathlib import Path

MM_TO_PT = 72.0 / 25.4


def _pt(mm: float) -> str:
    return f"{mm * MM_TO_PT:.4f}".rstrip("0").rstrip(".")


class PdfPage:
    def __init__(self, width_mm: float, height_mm: float):
        self.width_mm = width_mm
        self.height_mm = height_mm
        self._ops: list[str] = []

    def rect(self, x: float, y: float, w: float, h: float, gray: float = 0.0) -> None:
        """Filled rectangle; (x, y) is its top-left corner."""
        self._ops.append(
            f"{gray:g} g {_pt(x)} {_pt(self.height_mm - y - h)} {_pt(w)} {_pt(h)} re f"
        )

    def text(self, x: float, y: float, text: str, size_pt: float = 9.0) -> None:
        """Text with its baseline at y. ASCII only."""
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self._ops.append(
            f"0 g BT /F1 {size_pt:g} Tf {_pt(x)} {_pt(self.height_mm - y)} Td ({escaped}) Tj ET"
        )

    def content(self) -> bytes:
        return "\n".join(self._ops).encode("ascii")


def write_pdf(path: str | Path, pages: list[PdfPage]) -> None:
    # Object numbers: 1 catalog, 2 page tree, 3 font, then (page, content) pairs.
    page_ids = [4 + 2 * i for i in range(len(pages))]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + " ".join(f"{p} 0 R" for p in page_ids).encode()
        + b"] /Count " + str(len(pages)).encode() + b" >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for page_id, page in zip(page_ids, pages):
        box = f"[0 0 {_pt(page.width_mm)} {_pt(page.height_mm)}]"
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox {box} "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode()
        )
        stream = page.content()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    Path(path).write_bytes(bytes(out))
