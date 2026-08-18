from bgcc.models.enums import BGStatus, PlatformRole
from bgcc.services.workflow_service import DOA_ROLES


def user_participates_in_chain(user):
    return bool(set(user.granted_roles or []) & DOA_ROLES)


def can_view_bg(user, bg):
    """True if the user may view the BG's detail, documents and generated docs.

    Admins always pass. BG owners (creator or coordinator) always have access.
    Draft records are limited to the owner. Otherwise the user must share the
    BG's SAP system and hold at least one role that participates in the DoA chain.
    """
    if not user or not bg:
        return False
    if PlatformRole.admin.value in (user.granted_roles or []) or getattr(user, "active_role", None) == PlatformRole.admin.value:
        return True
    if user.id == bg.creator_id or user.id == bg.coordinator_id:
        return True
    st = bg.status.value if hasattr(bg.status, "value") else str(bg.status)
    if st == BGStatus.draft.value or st == "draft":
        return False
    if bg.sap_system_id and bg.sap_system_id == user.sap_system_id:
        if user_participates_in_chain(user):
            return True
    return False
