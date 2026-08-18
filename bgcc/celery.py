import os
from celery import Celery
from celery.schedules import crontab

celery = Celery("bgcc")


def init_celery(app):
    celery.conf.update(
        broker_url=app.config["CELERY_BROKER_URL"],
        result_backend=app.config["CELERY_RESULT_BACKEND"],
        task_always_eager=app.config["CELERY_TASK_ALWAYS_EAGER"],
        task_eager_propagates=app.config["CELERY_TASK_EAGER_PROPAGATES"],
        broker_connection_retry_on_startup=True,
        worker_pool="solo" if os.name == "nt" else "prefork",
        timezone=app.config.get("TIMEZONE", "UTC"),
        include=[
            "bgcc.tasks.notification_tasks",
            "bgcc.tasks.ai_tasks",
            "bgcc.tasks.workflow_tasks",
            "bgcc.tasks.invocation_tasks",
            "bgcc.tasks.maintenance_tasks",
            "bgcc.tasks.document_tasks",
        ],
        beat_schedule={
            "daily-expiry-scan": {
                "task": "maintenance.daily_expiry_scan",
                "schedule": crontab(hour=1, minute=0),
            },
            "daily-extension-digest": {
                "task": "maintenance.daily_extension_digest",
                "schedule": crontab(hour=8, minute=0),
            },
            "daily-claim-window-scan": {
                "task": "maintenance.daily_claim_window_scan",
                "schedule": crontab(hour=2, minute=0),
            },
            "hourly-dashboard-warm": {
                "task": "maintenance.warm_dashboard_cache",
                "schedule": crontab(minute=5),
            },
            "bank-verification-poll": {
                "task": "maintenance.bank_verification_poll",
                "schedule": crontab(minute="*/30"),
            },
        },
    )

    class ContextTask(celery.Task):
        abstract = True

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery
