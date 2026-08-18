from flask import Blueprint


def create_blueprints():
    """Build every blueprint for the current build stage.

    This step registers auth, dashboard and legal fully. The remaining
    blueprints are created now (empty) so later steps register into an
    already-wired structure instead of creating one from scratch.
    """
    from bgcc.routes import (
        admin,
        api,
        approval,
        assistant,
        auth,
        bg,
        dashboard,
        documents,
        hub,
        intake,
        invocation,
        legal,
        lifecycle,
        notifications,
        profile,
        reports,
    )

    return [
        auth.bp,
        dashboard.bp,
        legal.bp,
        intake.bp,
        approval.bp,
        bg.bp,
        lifecycle.bp,
        invocation.bp,
        hub.bp,
        documents.bp,
        reports.bp,
        assistant.bp,
        profile.bp,
        notifications.bp,
        admin.bp,
        api.bp,
    ]
