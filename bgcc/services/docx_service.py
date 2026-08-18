"""DOCX rendering for generated documents.

`render_invocation_letter` merges deterministic structured content into the
starter invocation-letter template using docxtpl placeholders. The template is a
versioned project asset (`bgcc/assets/invocation_letter_template.docx`), clearly
starter content a real organization replaces with its own legally reviewed
template. The binary file is never authored by an AI.
"""
import os

from docxtpl import DocxTemplate

TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "assets", "invocation_letter_template.docx"
)


def render_invocation_letter(content, output_path):
    template = DocxTemplate(os.path.normpath(TEMPLATE_PATH))
    context = {
        "sender_name": content.get("sender_name", ""),
        "sender_address": content.get("sender_address", ""),
        "date": content.get("date", ""),
        "recipient_bank": content.get("recipient_bank", ""),
        "recipient_branch": content.get("recipient_branch", ""),
        "recipient_address": content.get("recipient_address", ""),
        "bg_number": content.get("bg_number", ""),
        "vendor_name": content.get("vendor_name", ""),
        "guarantee_type_label": content.get("guarantee_type_label", ""),
        "claim_amount_figures": content.get("claim_amount_figures", ""),
        "claim_amount_words": content.get("claim_amount_words", ""),
        "claim_deadline": content.get("claim_deadline", ""),
        "invocation_phrasing": content.get("invocation_phrasing", ""),
        "signing_authority": content.get("signing_authority", ""),
    }
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    template.render(context)
    template.save(output_path)
    return output_path
