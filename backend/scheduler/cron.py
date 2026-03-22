"""
Scheduler module - manages cron jobs for the blog pipeline.
Uses APScheduler for built-in cron scheduling.
"""
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from backend import config

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def setup_scheduler(discovery_callback, reviewer_callback):
    """
    Set up the scheduler with:
    - Daily discovery job (configurable hour)
    - Periodic PR review polling
    """
    # Blog Discovery - runs daily at configured hour
    scheduler.add_job(
        discovery_callback,
        trigger=CronTrigger(
            hour=config.DISCOVERY_CRON_HOUR,
            minute=config.DISCOVERY_CRON_MINUTE,
        ),
        id="blog_discovery",
        name="Blog Topic Discovery",
        replace_existing=True,
        max_instances=1,
    )
    logger.info(
        f"Scheduled blog discovery: daily at {config.DISCOVERY_CRON_HOUR:02d}:{config.DISCOVERY_CRON_MINUTE:02d}"
    )

    # PR Review - runs at configured interval
    scheduler.add_job(
        reviewer_callback,
        trigger=IntervalTrigger(seconds=config.PR_POLL_INTERVAL_SECONDS),
        id="pr_review",
        name="PR Review Polling",
        replace_existing=True,
        max_instances=1,
    )
    logger.info(f"Scheduled PR review polling: every {config.PR_POLL_INTERVAL_SECONDS}s")


def start_scheduler():
    """Start the scheduler."""
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def stop_scheduler():
    """Gracefully stop the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped")
