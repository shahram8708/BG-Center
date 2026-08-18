"""BG extension lifecycle helpers (Step 4)."""
from datetime import datetime

from bgcc.extensions import db
from bgcc.models.enums import WorkflowAction
from bgcc.models.lifecycle import ExtensionRequest
from bgcc.models.workflow import WorkflowHistory
from bgcc.services import audit_service


def initiate_extension(bg, coordinator, vendor_email, message=None):
    """Create/update an extension_request and dispatch the vendor email."""
    vendor_email = (vendor_email or "").strip()
    if not vendor_email:
        raise ValueError("A vendor contact email is required to request an extension.")

    req = (
        ExtensionRequest.query.filter_by(parent_bg_id=bg.id)
        .order_by(ExtensionRequest.id.desc())
        .first()
    )
    if req is None or req.stage == "uploaded":
        req = ExtensionRequest(parent_bg_id=bg.id, stage="not_started", coordinator_id=coordinator.id)
        db.session.add(req)
    elif req.stage == "requested":
        pass  # re-requesting updates the same open request
    req.vendor_email = vendor_email
    req.stage = "requested"
    req.requested_at = datetime.utcnow()
    req.is_overdue = False
    if req.coordinator_id is None:
        req.coordinator_id = coordinator.id
    db.session.flush()

    db.session.add(WorkflowHistory(
        bank_guarantee_id=bg.id, from_stage=bg.status.value, to_stage="extension_requested",
        action=WorkflowAction.extension_requested, actor_id=coordinator.id,
        actor_role=coordinator.active_role,
        comments=message or "Extension requested from vendor.",
    ))
    db.session.commit()

    audit_service.record(
        "extension_requested", actor_id=coordinator.id, target_type="bank_guarantee",
        target_id=bg.id,
        metadata_json={"bg_number": bg.bg_number, "vendor_email": vendor_email},
    )

    from bgcc.tasks.workflow_tasks import send_vendor_extension_email

    send_vendor_extension_email.delay(
        vendor_email=vendor_email,
        bg_number=bg.bg_number,
        vendor_name=bg.vendor_name or "our vendor",
        expiry_date=str(bg.expiry_date),
        message=message or "",
    )
    return req


def link_uploaded_extension(parent_bg, child_bg):
    """Additively link a newly-uploaded extended BG to its parent's request."""
    req = (
        ExtensionRequest.query.filter_by(parent_bg_id=parent_bg.id)
        .order_by(ExtensionRequest.id.desc())
        .first()
    )
    if req is None:
        # Coordinator used the manual-search entry point without initiating first.
        req = ExtensionRequest(
            parent_bg_id=parent_bg.id, stage="uploaded", coordinator_id=child_bg.coordinator_id
        )
        db.session.add(req)
        db.session.flush()
    else:
        req.stage = "uploaded"
        req.child_bg_id = child_bg.id
    if req.child_bg_id is None:
        req.child_bg_id = child_bg.id
    db.session.add(WorkflowHistory(
        bank_guarantee_id=parent_bg.id, from_stage="extension_requested",
        to_stage="extension_uploaded", action=WorkflowAction.extension_uploaded,
        actor_id=child_bg.coordinator_id or child_bg.creator_id,
        actor_role="coordinator",
        comments=f"Extended BG {child_bg.bg_number} uploaded against parent.",
    ))
    db.session.commit()
    return req
