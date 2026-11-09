from pathlib import Path
import re
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Preformatted, PageBreak, KeepTogether
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.colors import HexColor

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs'

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name='DocTitle', parent=styles['Title'], fontName='Helvetica-Bold', fontSize=25, leading=30, textColor=HexColor('#193d35'), spaceAfter=12, alignment=TA_LEFT))
styles.add(ParagraphStyle(name='Subtitle', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=13, textColor=HexColor('#5d706a'), spaceAfter=18))
styles.add(ParagraphStyle(name='H1x', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=17, leading=21, textColor=HexColor('#193d35'), spaceBefore=15, spaceAfter=8, keepWithNext=True))
styles.add(ParagraphStyle(name='H2x', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=12.5, leading=16, textColor=HexColor('#315f53'), spaceBefore=11, spaceAfter=5, keepWithNext=True))
styles.add(ParagraphStyle(name='H3x', parent=styles['Heading3'], fontName='Helvetica-Bold', fontSize=10.5, leading=14, textColor=HexColor('#315f53'), spaceBefore=8, spaceAfter=4, keepWithNext=True))
styles.add(ParagraphStyle(name='Bodyx', parent=styles['BodyText'], fontName='Helvetica', fontSize=9.1, leading=13.4, textColor=HexColor('#26332f'), spaceAfter=6))
styles.add(ParagraphStyle(name='Bulletx', parent=styles['BodyText'], fontName='Helvetica', fontSize=9, leading=12.8, leftIndent=13, firstLineIndent=-8, bulletIndent=0, textColor=HexColor('#26332f'), spaceAfter=3))
styles.add(ParagraphStyle(name='Smallx', parent=styles['BodyText'], fontName='Helvetica', fontSize=7.8, leading=10, textColor=HexColor('#26332f')))
styles.add(ParagraphStyle(name='Codex', fontName='Courier', fontSize=7.5, leading=10, leftIndent=7, rightIndent=7, backColor=HexColor('#f3f5f2'), borderColor=HexColor('#dce3dd'), borderWidth=.5, borderPadding=7, spaceBefore=3, spaceAfter=8))


def inline(text):
    text = escape(text.strip())
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<font color="#315f53"><u>\1</u></font>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'`([^`]+)`', r'<font name="Courier" size="8">\1</font>', text)
    return text


def on_page(canvas, doc):
    canvas.saveState()
    w, h = A4
    canvas.setStrokeColor(HexColor('#dce3dd'))
    canvas.line(18*mm, 15*mm, w-18*mm, 15*mm)
    canvas.setFont('Helvetica', 7.5)
    canvas.setFillColor(HexColor('#6c7c76'))
    canvas.drawString(18*mm, 10*mm, 'Study Desk  |  Project documentation')
    canvas.drawRightString(w-18*mm, 10*mm, f'{doc.page}')
    canvas.restoreState()


def make_pdf(md_path):
    text = md_path.read_text(encoding='utf-8')
    lines = text.splitlines()
    story = []
    i = 0
    title_seen = False
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.startswith('```'):
            i += 1
            block = []
            while i < len(lines) and not lines[i].startswith('```'):
                block.append(lines[i])
                i += 1
            i += 1
            story.append(Preformatted('\n'.join(block), styles['Codex']))
            continue
        if line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = [c.strip() for c in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-{3,}:?', c.replace(' ', '')) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                max_cols = max(len(row) for row in rows)
                rows = [row + ['']*(max_cols-len(row)) for row in rows]
                data = [[Paragraph(inline(c), styles['Smallx']) for c in row] for row in rows]
                widths = [ (A4[0]-36*mm)/max_cols ]*max_cols
                tbl = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
                tbl.setStyle(TableStyle([
                    ('BACKGROUND',(0,0),(-1,0),HexColor('#e8efeb')),
                    ('TEXTCOLOR',(0,0),(-1,0),HexColor('#193d35')),
                    ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
                    ('GRID',(0,0),(-1,-1),.35,HexColor('#d5ded8')),
                    ('VALIGN',(0,0),(-1,-1),'TOP'),
                    ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
                    ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
                    ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,HexColor('#fafbf9')]),
                ]))
                story.extend([Spacer(1,3),tbl,Spacer(1,7)])
            continue
        if line.startswith('# '):
            story.append(Paragraph(inline(line[2:]), styles['DocTitle'] if not title_seen else styles['H1x']))
            if not title_seen:
                title_seen = True
            i += 1
            continue
        if line.startswith('## '):
            # metadata and subtitle are compact under title
            val = line[3:].strip()
            story.append(Paragraph(inline(val), styles['Subtitle'] if val.startswith('Status:') or val.startswith('Updated:') or val.startswith('Product:') else styles['H1x']))
            i += 1
            continue
        if line.startswith('### '):
            story.append(Paragraph(inline(line[4:]), styles['H2x']))
            i += 1
            continue
        if line.startswith('#### '):
            story.append(Paragraph(inline(line[5:]), styles['H3x']))
            i += 1
            continue
        if line.startswith('---'):
            story.append(Spacer(1,5)); i+=1; continue
        if re.match(r'^\s*[-*] ', line):
            story.append(Paragraph('• ' + inline(re.sub(r'^\s*[-*] ', '', line)), styles['Bulletx']))
            i += 1
            continue
        if re.match(r'^\s*\d+\. ', line):
            m = re.match(r'^\s*(\d+)\. (.*)', line)
            story.append(Paragraph(f'{m.group(1)}. ' + inline(m.group(2)), styles['Bulletx']))
            i += 1
            continue
        if line.startswith('> '):
            story.append(Paragraph(inline(line[2:]), styles['Bodyx']))
            i += 1
            continue
        story.append(Paragraph(inline(line), styles['Bodyx']))
        i += 1
    out = md_path.with_suffix('.pdf')
    doc = SimpleDocTemplate(str(out), pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=18*mm, bottomMargin=21*mm, title=md_path.stem.replace('-', ' '), author='Study Desk')
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f'{out.name}: {out.stat().st_size} bytes')

for name in ['PRD.md','CHALLENGES-AND-FIXES.md','CODEBASE-GUIDE.md']:
    make_pdf(DOCS/name)
