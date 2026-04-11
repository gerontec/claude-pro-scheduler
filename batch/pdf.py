"""PdfRenderer — konvertiert Markdown-Ergebnis zu PDF (fpdf2, Unicode)."""
import os
import re
import tempfile
from fpdf import FPDF
from PIL import Image
from fpdf.enums import XPos, YPos

FONT_REGULAR = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD    = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONT_MONO    = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'


class PdfRenderer:
    """Rendert einen Job-Ergebnis-Text (Markdown) als PDF-Bytes."""

    MARGIN = 15
    WIDTH  = 210  # A4
    FONT   = 'DejaVu'
    MONO   = 'DejaVuMono'

    def render(self, job_id: int, model: str, status: str,
               cost, result: str, png_bytes: bytes | None = None) -> bytes:
        pdf = FPDF()
        pdf.set_margins(self.MARGIN, self.MARGIN, self.MARGIN)
        pdf.add_font(self.FONT,        '', FONT_REGULAR)
        pdf.add_font(self.FONT,        'B', FONT_BOLD)
        pdf.add_font(self.MONO,        '', FONT_MONO)
        pdf.add_page()

        # ── Header ──────────────────────────────────────────────
        pdf.set_font(self.FONT, 'B', 14)
        pdf.cell(0, 8, f'KI-Job #{job_id} \u2014 {status.upper()}',
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font(self.FONT, '', 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 5, f'Modell: {model}   Kosten: ${cost or "n/a"}',
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)
        pdf.set_draw_color(180, 180, 180)
        pdf.line(self.MARGIN, pdf.get_y(), self.WIDTH - self.MARGIN, pdf.get_y())
        pdf.ln(4)

        # ── Inhalt ───────────────────────────────────────────────
        for line in (result or '').splitlines():
            self._render_line(pdf, line)

        # ── Diagramm einbetten (optional) ─────────────────────
        if png_bytes:
            self._embed_diagram(pdf, png_bytes)

        return pdf.output()

    def _embed_diagram(self, pdf, png_bytes):
        tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        try:
            tmp.write(png_bytes)
            tmp.flush()
            w_px, h_px = Image.open(tmp.name).size
            if w_px > 2000 or h_px > 1400:
                page_w, page_h = 420, 297
            else:
                page_w, page_h = 297, 210
            pdf.add_page(orientation='L', format=(page_w, page_h))
            pdf.set_font(self.FONT, 'B', 14)
            pdf.cell(0, 8, 'Anhang: Klassendiagramm',
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(4)
            margin = 10
            avail_w = page_w - 2 * margin
            avail_h = page_h - pdf.get_y() - margin
            pdf.image(tmp.name, x=margin, y=pdf.get_y(),
                      w=avail_w, h=avail_h)
        finally:
            tmp.close()
            os.unlink(tmp.name)

    def _render_line(self, pdf: FPDF, line: str) -> None:
        usable = self.WIDTH - 2 * self.MARGIN

        # H1 ##
        if line.startswith('# ') and not line.startswith('## '):
            pdf.ln(2)
            pdf.set_font(self.FONT, 'B', 13)
            pdf.set_text_color(30, 30, 150)
            pdf.multi_cell(usable, 7, line[2:].strip(),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(1)

        # H2 ##
        elif line.startswith('## ') and not line.startswith('### '):
            pdf.ln(3)
            pdf.set_font(self.FONT, 'B', 11)
            pdf.set_text_color(50, 50, 160)
            pdf.multi_cell(usable, 6, line[3:].strip(),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_draw_color(200, 200, 220)
            pdf.line(self.MARGIN, pdf.get_y(),
                     self.WIDTH - self.MARGIN, pdf.get_y())
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)

        # H3 ###
        elif line.startswith('### '):
            pdf.ln(2)
            pdf.set_font(self.FONT, 'B', 10)
            pdf.multi_cell(usable, 6, line[4:].strip(),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)

        # Code-Block (``` Zeilen)
        elif line.startswith('```') or line.startswith('    '):
            pdf.set_font(self.MONO, '', 8)
            pdf.set_fill_color(240, 240, 240)
            text = line.lstrip('`').lstrip()
            if text:
                pdf.multi_cell(usable, 5, text, fill=True,
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font(self.FONT, '', 10)


        # Bullet-Punkte - * •
        elif re.match(r'^[-*•] ', line):
            pdf.set_font(self.FONT, '', 10)
            pdf.set_x(self.MARGIN + 4)
            pdf.multi_cell(usable - 4, 5,
                           '• ' + line[2:].strip(),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # Nummerierte Liste
        elif re.match(r'^\d+\. ', line):
            pdf.set_font(self.FONT, '', 10)
            pdf.set_x(self.MARGIN + 4)
            pdf.multi_cell(usable - 4, 5, line.strip(),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # Trennlinie ---
        elif re.match(r'^-{3,}$', line.strip()):
            pdf.ln(2)
            pdf.set_draw_color(180, 180, 180)
            pdf.line(self.MARGIN, pdf.get_y(),
                     self.WIDTH - self.MARGIN, pdf.get_y())
            pdf.ln(2)

        # Leerzeile
        elif not line.strip():
            pdf.ln(2)

        # Normaler Text (inline **bold** entfernen)
        else:
            pdf.set_font(self.FONT, '', 10)
            clean = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
            clean = re.sub(r'\*(.+?)\*',     r'\1', clean)
            clean = re.sub(r'`(.+?)`',       r'\1', clean)
            pdf.multi_cell(usable, 5, clean.strip(),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
