from pathlib import Path
import re
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs'
GREEN = '315F53'
INK = '26332F'
PALE = 'E8EFEB'


def shade(cell, fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:fill'), fill)
    tcPr.append(shd)


def inline(paragraph, text):
    # Preserve simple Markdown emphasis, code spans, and link labels.
    pattern = re.compile(r'(\*\*.*?\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))')
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        token = m.group(0)
        if token.startswith('**'):
            r = paragraph.add_run(token[2:-2]); r.bold = True
        elif token.startswith('*'):
            r = paragraph.add_run(token[1:-1]); r.italic = True
        elif token.startswith('`'):
            r = paragraph.add_run(token[1:-1]); r.font.name = 'Consolas'; r.font.size = Pt(9)
        else:
            label = re.match(r'\[([^\]]+)\]', token).group(1)
            r = paragraph.add_run(label); r.font.color.rgb = RGBColor.from_string(GREEN); r.underline = True
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def set_cell_text(cell, text, header=False):
    cell.text = ''
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(1)
    inline(p, text)
    for run in p.runs:
        run.font.name = 'Calibri'
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor.from_string(INK)
        if header: run.bold = True
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    if header: shade(cell, PALE)


def add_page_field(p):
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run('Study Desk  |  ')
    run.font.size = Pt(8); run.font.color.rgb = RGBColor(105,120,113)
    fld = OxmlElement('w:fldSimple'); fld.set(qn('w:instr'), 'PAGE'); p._p.append(fld)


def convert(md_path):
    lines = md_path.read_text(encoding='utf-8').splitlines()
    doc = Document()
    sec = doc.sections[0]
    sec.top_margin = Inches(.72); sec.bottom_margin = Inches(.68)
    sec.left_margin = Inches(.78); sec.right_margin = Inches(.78)
    normal = doc.styles['Normal']; normal.font.name = 'Calibri'; normal.font.size = Pt(10); normal.font.color.rgb = RGBColor.from_string(INK)
    normal.paragraph_format.space_after = Pt(6)
    for sname, size, color in [('Heading 1',17,'193D35'),('Heading 2',13,GREEN),('Heading 3',11,GREEN)]:
        st = doc.styles[sname]; st.font.name='Calibri'; st.font.size=Pt(size); st.font.bold=True; st.font.color.rgb=RGBColor.from_string(color)
        st.paragraph_format.keep_with_next = True
    title = doc.styles['Title']; title.font.name='Calibri'; title.font.size=Pt(26); title.font.bold=True; title.font.color.rgb=RGBColor.from_string('193D35')
    title.paragraph_format.space_after = Pt(12)
    add_page_field(sec.footer.paragraphs[0])

    i=0; first_title=True
    while i<len(lines):
        line=lines[i].rstrip()
        if not line.strip(): i+=1; continue
        if line.startswith('```'):
            i+=1; block=[]
            while i<len(lines) and not lines[i].startswith('```'):
                block.append(lines[i]); i+=1
            i+=1
            p=doc.add_paragraph()
            p.paragraph_format.left_indent=Inches(.12); p.paragraph_format.right_indent=Inches(.1)
            p.paragraph_format.space_before=Pt(3); p.paragraph_format.space_after=Pt(8)
            pPr=p._p.get_or_add_pPr(); shd=OxmlElement('w:shd'); shd.set(qn('w:fill'),'F3F5F2'); pPr.append(shd)
            r=p.add_run('\n'.join(block)); r.font.name='Consolas'; r.font.size=Pt(8)
            continue
        if line.startswith('|'):
            raw=[]
            while i<len(lines) and lines[i].strip().startswith('|'):
                cells=[c.strip() for c in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-{3,}:?', c.replace(' ','')) for c in cells): raw.append(cells)
                i+=1
            if raw:
                n=max(map(len,raw)); table=doc.add_table(rows=len(raw), cols=n)
                table.alignment=WD_TABLE_ALIGNMENT.CENTER; table.style='Table Grid'; table.autofit=True
                for ri,row in enumerate(raw):
                    for ci in range(n): set_cell_text(table.cell(ri,ci), row[ci] if ci<len(row) else '', header=(ri==0))
                doc.add_paragraph().paragraph_format.space_after=Pt(1)
            continue
        if line.startswith('# '):
            p=doc.add_paragraph(style='Title' if first_title else 'Heading 1'); inline(p,line[2:]); first_title=False; i+=1; continue
        if line.startswith('## '):
            value=line[3:].strip()
            if value.startswith(('Status:','Updated:','Product:')):
                p=doc.add_paragraph(); p.paragraph_format.space_after=Pt(3)
                inline(p,value)
                for r in p.runs: r.font.size=Pt(9); r.font.color.rgb=RGBColor(105,120,113)
            else:
                p=doc.add_paragraph(style='Heading 1'); inline(p,value)
            i+=1; continue
        if line.startswith('### '):
            p=doc.add_paragraph(style='Heading 2'); inline(p,line[4:]); i+=1; continue
        if line.startswith('#### '):
            p=doc.add_paragraph(style='Heading 3'); inline(p,line[5:]); i+=1; continue
        if re.match(r'^\s*[-*] ',line):
            p=doc.add_paragraph(style='List Bullet'); p.paragraph_format.space_after=Pt(2); inline(p,re.sub(r'^\s*[-*] ','',line)); i+=1; continue
        if re.match(r'^\s*\d+\. ',line):
            p=doc.add_paragraph(style='List Number'); p.paragraph_format.space_after=Pt(2); inline(p,re.sub(r'^\s*\d+\. ','',line)); i+=1; continue
        if line.startswith('---'): i+=1; continue
        p=doc.add_paragraph(); inline(p,line); i+=1
    out=md_path.with_suffix('.docx'); doc.save(out); print(out.name, out.stat().st_size)

for fn in ['PRD.md','CHALLENGES-AND-FIXES.md','CODEBASE-GUIDE.md']:
    convert(DOCS/fn)
