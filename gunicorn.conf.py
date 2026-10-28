
def post_worker_init(worker):
    """Start background campaign processing after the web worker initializes."""
    from email_worker import campaign_manager

    worker.log.info(
        "[GUNICORN] Starting campaign manager in worker pid=%s",
        worker.pid,
    )
    campaign_manager.start()
