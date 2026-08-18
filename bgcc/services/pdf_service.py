"""PDF rendering for generated documents.

`render_invocation_letter_pdf` is an independent rendering of the exact same
structured content as the DOCX, produced directly with ReportLab (no DOCX-to-PDF
toolchain is reliably available in the build environment). Both files are real,
complete renderings; the DOCX is authoritative for printing/signature.
"""
import os
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

def _styles():
    base = getSampleStyleSheet()
    title = ParagraphStyle("InvTitle", parent=base["Title"], fontSize=13, alignment=1, spaceAfter=2)
    sub = ParagraphStyle("InvSub", parent=base["Normal"], fontSize=9, textColor=colors.HexColor("#374151"), alignment=1, spaceAfter=14)
    body = ParagraphStyle("InvBody", parent=base["Normal"], fontSize=10.5, leading=15)
    bold = ParagraphStyle("InvBold", parent=body, fontName="Helvetica-Bold")
    right = ParagraphStyle("InvRight", parent=body, alignment=TA_LEFT)
    return title, sub, body, bold, right


def render_invocation_letter_pdf(content, output_path):
    title_s, sub_s, body_s, bold_s, right_s = _styles()
    elements = []
    elements.append(Paragraph(content.get("sender_name", ""), title_s))
    elements.append(Paragraph(content.get("sender_address", ""), sub_s))
    elements.append(Paragraph(f"Date: {content.get('date', '')}", right_s))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph("To,<br/>The Manager,", body_s))
    elements.append(Paragraph(content.get("recipient_bank", ""), bold_s))
    elements.append(Paragraph(content.get("recipient_branch", ""), body_s))
    elements.append(Paragraph(content.get("recipient_address", ""), body_s))
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(
        f"Sub: Demand under Bank Guarantee {content.get('bg_number', '')} - {content.get('vendor_name', '')}",
        bold_s,
    ))
    elements.append(Spacer(1, 8))
    elements.append(Paragraph("Dear Sir/Madam,", body_s))
    elements.append(Spacer(1, 8))
    elements.append(Paragraph(
        f"We refer to the above-mentioned Bank Guarantee bearing number "
        f"{content.get('bg_number', '')} issued in favour of us by your bank on behalf of "
        f"our vendor M/s. {content.get('vendor_name', '')}. The said guarantee is of the type "
        f"{content.get('guarantee_type_label', '')} and covers the obligations of the vendor "
        f"towards us as per the terms of the underlying contract.", body_s,
    ))
    elements.append(Paragraph(
        f"Whereas the said vendor has failed to discharge its obligations thereunder, we "
        f"hereby invoke the said Bank Guarantee and make a claim for the sum of "
        f"{content.get('claim_amount_figures', '')} ({content.get('claim_amount_words', '')}). "
        f"In this regard, we rely on the following invocation:", body_s,
    ))
    elements.append(Spacer(1, 4))
    elements.append(Paragraph(content.get("invocation_phrasing", ""), body_s))
    elements.append(Paragraph(
        f"We call upon your bank to remit the said amount to us without demur or protest, "
        f"on first demand, in terms of the guarantee. This demand is made within the "
        f"validity period of the guarantee and the claim must be settled on or before "
        f"{content.get('claim_deadline', '')}.", body_s,
    ))
    elements.append(Paragraph(
        "This notice is issued without prejudice to our other rights and remedies.", body_s,
    ))
    elements.append(Spacer(1, 14))
    elements.append(Paragraph("Yours faithfully,", body_s))
    elements.append(Spacer(1, 24))
    elements.append(Paragraph(f"For {content.get('sender_name', '')}", bold_s))
    elements.append(Paragraph(content.get("signing_authority", ""), bold_s))
    elements.append(Paragraph("Authorised Signatory", body_s))

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    doc.build(elements)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(buf.getvalue())
    return output_path
