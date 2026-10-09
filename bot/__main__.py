"""Entry point: python -m bot"""
from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from telegram import Update
from telegram.ext import Application

from .config import Config
from .db import DB
from .features import FeatureMapper
from .google_services import GoogleServices
from .handlers import register_handlers, reminder_job, set_commands
from .services import AppServices
from .webhook import build_web_app

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")


def build_application(cfg: Config) -> Application:
    builder = (
        Application.builder()
        .token(cfg.telegram_token)
        .read_timeout(60)
        .write_timeout(120)
        .media_write_timeout(300)
    )
    if cfg.telegram_api_base_url:
        builder = (builder.base_url(f"{cfg.telegram_api_base_url}/bot")
                   .base_file_url(f"{cfg.telegram_api_base_url}/file/bot")
                   .local_mode(cfg.telegram_local_mode))
    return builder.build()


async def main() -> None:
    cfg = Config.from_env()
    svc = AppServices(
        cfg=cfg,
        db=DB(cfg.db_path),
        mapper=FeatureMapper(cfg.aliases_file),
        google=GoogleServices.from_config(cfg),
    )
    app = build_application(cfg)
    app.bot_data["svc"] = svc
    register_handlers(app)

    runner = web.AppRunner(build_web_app(svc, app.bot))
    async with app:
        await set_commands(app)
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        app.job_queue.run_repeating(reminder_job, interval=1800, first=120)

        await runner.setup()
        await web.TCPSite(runner, cfg.webhook_host, cfg.webhook_port).start()
        log.info("Bot @%s jalan · webhook di http://%s:%s%s",
                 app.bot.username, cfg.webhook_host, cfg.webhook_port, cfg.webhook_path)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        log.info("Mematikan bot…")
        await runner.cleanup()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
