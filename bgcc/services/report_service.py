"""Deterministic Comparison Report PDF generation.

Pure rendering of already-computed deviation data - no AI call. Regenerating
produces a new versioned `generated_documents` row rather than overwriting the
previous one, preserving history.
"""
import os
import time
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from bgcc.extensions import db
from bgcc.models.deviations import Deviation
from bgcc.models.generated_documents import GeneratedDocument
from bgcc.services.workflow_service import role_label

_TIER_LABEL = {"low": "Low", "high": "High", "prohibited": "Prohibited"}
_STATUS_LABEL = {"pending": "Pending", "accepted": "Accepted", "rejected": "Rejected"}


def _styles():
    base = getSampleStyleSheet()
    title = ParagraphStyle("RepTitle", parent=base["Title"], fontSize=16, spaceAfter=4)
    sub = ParagraphStyle("RepSub", parent=base["Normal"], textColor=colors.HexColor("#374151"), fontSize=9, spaceAfter=12)
    h2 = ParagraphStyle("RepH2", parent=base["Heading2"], fontSize=11, textColor=colors.HexColor("#7C3AED"), spaceBefore=10, spaceAfter=4)
    cell = ParagraphStyle("RepCell", parent=base["Normal"], fontSize=8.5, leading=11)
    return title, sub, h2, cell


def generate_comparison_report(bg, user, output_root):
    title_s, sub_s, h2_s, cell_s = _styles()
    deviations = (
        Deviation.query.filter_by(bank_guarantee_id=bg.id).order_by(Deviation.id).all()
    )

    elements = []
    elements.append(Paragraph(f"Bank Guarantee Comparison Report", title_s))
    elements.append(Paragraph(
        f"BG {bg.bg_number} · {bg.vendor_name or 'vendor not set'} · "
        f"{bg.amount} {bg.currency} · Generated {time.strftime('%d %b %Y %H:%M')}",
        sub_s,
    ))

    elements.append(Paragraph("Summary", h2_s))
    summary_data = [
        ["BG number", bg.bg_number],
        ["Vendor / beneficiary", bg.vendor_name or "-"],
        ["Amount", f"{bg.amount} {bg.currency}"],
        ["Issue date", str(bg.issue_date)],
        ["Expiry date", str(bg.expiry_date)],
        ["Issuing bank", bg.issuing_bank or "-"],
        ["Risk summary", bg.risk_tier_summary or "-"],
    ]
    elements.append(_table(summary_data, col_widths=[60 * mm, 120 * mm], cell=cell_s))

    elements.append(Paragraph("Clause Deviations", h2_s))
    header = ["Clause ref", "Type", "AI tier", "Effective tier", "Status", "Decision comment"]
    rows = [header]
    for d in deviations:
        rows.append([
            Paragraph(str(d.clause_reference), cell_s),
            Paragraph(str(d.deviation_type or "-").replace("_", " ").title(), cell_s),
            Paragraph(_TIER_LABEL.get(d.ai_proposed_tier, "-"), cell_s),
            Paragraph(_TIER_LABEL.get(d.effective_tier, "-"), cell_s),
            Paragraph(_STATUS_LABEL.get(d.status, "-"), cell_s),
            Paragraph(str(d.decision_comment or ""), cell_s),
        ])
    elements.append(_table(rows, col_widths=[28 * mm, 34 * mm, 24 * mm, 28 * mm, 28 * mm, 38 * mm],
                           cell=cell_s, header=True))

    missing = [d for d in deviations if d.is_missing_critical_clause]
    if missing:
        elements.append(Paragraph("Missing Critical Clauses", h2_s))
        for d in missing:
            elements.append(Paragraph(
                f"• {d.clause_reference} - {d.template_text_summary}", cell_s
            ))

    footer = Paragraph(
        "This report is an internal platform record of the AI-assisted clause "
        "comparison. It is not legal advice. Decisions remain with the "
        "authorized approvers of the delegation-of-authority workflow.",
        ParagraphStyle("foot", parent=sub_s, fontSize=7.5, textColor=colors.grey),
    )
    elements.append(Spacer(1, 8))
    elements.append(footer)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
    )
    doc.build(elements)

    os.makedirs(output_root, exist_ok=True)
    filename = f"comparison_{bg.id}_{int(time.time())}.pdf"
    path = os.path.join(output_root, filename)
    with open(path, "wb") as f:
        f.write(buf.getvalue())

    prior = (
        GeneratedDocument.query.filter_by(
            bank_guarantee_id=bg.id, document_kind="comparison_report"
        ).order_by(GeneratedDocument.version.desc()).first()
    )
    version = (prior.version + 1) if prior else 1
    row = GeneratedDocument(
        bank_guarantee_id=bg.id,
        document_kind="comparison_report",
        storage_path=path,
        file_format="pdf",
        generated_by_user_id=user.id,
        version=version,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _table(rows, col_widths, cell, header=False):
    data = [[Paragraph(c, cell) if isinstance(c, str) else c for c in r] for r in rows]
    table = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F5F3FF")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
    table.setStyle(TableStyle(style))
    return table
