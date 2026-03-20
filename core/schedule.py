import zoneinfo
from collections.abc import Awaitable, Callable

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

TaskCallable = Callable[[], Awaitable[object | None]]
OutfitChangeCallable = Callable[[str], Awaitable[object | None]]


class LifeScheduler:
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        task: TaskCallable,
        outfit_change_task: OutfitChangeCallable | None = None,
    ):
        self.config = config
        self.task = task
        self.outfit_change_task = outfit_change_task
        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )
        self.scheduler = AsyncIOScheduler(
            timezone=self.timezone,
            executors={"default": AsyncIOExecutor()},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 120,
            },
        )
        self.job = None

    def start(self):
        try:
            schedule_time = self.config["schedule_time"]
            hour, minute = map(int, schedule_time.split(":"))
            self.job = self.scheduler.add_job(
                self.task,
                "cron",
                hour=hour,
                minute=minute,
                id="daily_schedule_gen",
            )
            # 添加换装定时任务
            self._setup_outfit_change_jobs()
            self.scheduler.start()
            logger.info(f"生活调度器已启动，时间：{schedule_time}")
        except Exception as e:
            logger.error(f"调度器初始化失败：{e}")

    def _setup_outfit_change_jobs(self):
        """根据配置添加定时换装任务"""
        if not self.outfit_change_task:
            return

        oc = self.config.get("outfit_change_schedule", {})

        schedule_map = {
            "noon": ("noon_change_time", "12:00", "enable_noon_change", "noon_change_hint"),
            "evening": ("evening_change_time", "18:00", "enable_evening_change", "evening_change_hint"),
            "night": ("night_change_time", "22:00", "enable_night_change", "night_change_hint"),
        }

        for name, (time_key, default_time, enable_key, hint_key) in schedule_map.items():
            time_str = oc.get(time_key, default_time)
            try:
                hour, minute = map(int, time_str.split(":"))
            except (ValueError, AttributeError):
                hour, minute = map(int, default_time.split(":"))
                logger.warning(f"换装时间格式错误: {time_str}，使用默认值 {default_time}")
            if oc.get(enable_key, False):
                hint = oc.get(hint_key, "")
                job_id = f"outfit_change_{name}"
                # 移除旧任务（如果存在）
                existing = self.scheduler.get_job(job_id)
                if existing:
                    self.scheduler.remove_job(job_id)
                self.scheduler.add_job(
                    self.outfit_change_task,
                    "cron",
                    hour=hour,
                    minute=minute,
                    id=job_id,
                    args=[hint],
                )
                logger.info(f"换装任务已添加: {name} ({hour:02d}:{minute:02d})")
            else:
                # 确保禁用的任务被移除
                existing = self.scheduler.get_job(f"outfit_change_{name}")
                if existing:
                    self.scheduler.remove_job(f"outfit_change_{name}")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()

    def update_schedule_time(self, new_time: str):
        if new_time == self.config["schedule_time"]:
            return

        try:
            hour, minute = map(int, new_time.split(":"))
            self.config["schedule_time"] = new_time
            self.config.save_config()
            if self.job:
                self.job.reschedule("cron", hour=hour, minute=minute)
                logger.info(f"生活调度器已重新排程至 {hour}:{minute}")
        except Exception as e:
            logger.error(f"更新调度器失败：{e}")
