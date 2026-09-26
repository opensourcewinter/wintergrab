"""A minimal PDF writer for tests: text in Helvetica at given places, and link annotations (no dependencies)."""


def make_pdf(pages, *, title=None, width=612, height=792):
    """``pages``: a list of pages, each a list of ``(x, y_from_top, size, text)`` or ``("link", x, y, w, h, uri)``."""
    objects = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    kids = []
    for page in pages:
        ops = []
        annots = []
        for item in page:
            if item[0] == "link":
                _, x, y, w, h, uri = item
                rect = f"[{x} {height - y - h} {x + w} {height - y}]"
                annots.append(
                    add(
                        f"<< /Type /Annot /Subtype /Link /Rect {rect} /Border [0 0 0] "
                        f"/A << /S /URI /URI ({uri}) >> >>".encode()
                    )
                )
                continue
            x, y, size, text, *style = item
            name = "/F2" if style and style[0] == "bold" else "/F1"
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"BT {name} {size} Tf 1 0 0 1 {x} {height - y} Tm ({escaped}) Tj ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        annot = (" /Annots [" + " ".join(f"{a} 0 R" for a in annots) + "]") if annots else ""
        kids.append(
            add(
                f"<< /Type /Page /Parent PAGES 0 R /MediaBox [0 0 {width} {height}] "
                f"/Resources << /Font << /F1 {font} 0 R /F2 {bold} 0 R >> >> /Contents {content} 0 R{annot} >>".encode()
            )
        )
    pages_obj = add(
        ("<< /Type /Pages /Kids [" + " ".join(f"{k} 0 R" for k in kids) + f"] /Count {len(kids)} >>").encode()
    )
    objects[:] = [o.replace(b"PAGES 0 R", f"{pages_obj} 0 R".encode()) for o in objects]
    info = add(f"<< /Title ({title}) >>".encode()) if title else None
    catalog = add(f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode())
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R" + (f" /Info {info} 0 R" if info else "") + " >>\n"
    )
    out += trailer.encode() + f"startxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
