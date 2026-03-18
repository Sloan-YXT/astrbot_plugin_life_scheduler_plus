import datetime
import re

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

from .core.data import ScheduleData, ScheduleDataManager
from .core.generator import SchedulerGenerator
from .core.schedule import LifeScheduler
from .core.utils import time_desc


class LifeSchedulerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.schedule_data_file = self.data_dir / "schedule_data.json"

    async def initialize(self):
        self.data_mgr = ScheduleDataManager(self.schedule_data_file)
        self.generator = SchedulerGenerator(self.context, self.config, self.data_mgr)
        self.scheduler = LifeScheduler(
            context=self.context,
            config=self.config,
            task=self.generator.generate_schedule,
            outfit_change_task=self._outfit_change,
        )
        self.scheduler.start()

    async def terminate(self):
        """插件卸载时清理"""
        self.scheduler.stop()

    # ---------- 内部工具 ----------

    def _get_latest_data(self) -> ScheduleData | None:
        """获取今日数据，若无则向前回退最多 3 天"""
        now = datetime.datetime.now()
        for offset in range(4):
            data = self.data_mgr.get(now - datetime.timedelta(days=offset))
            if data and data.status != "failed":
                return data
        return None

    async def _generate_outfit(self, hint: str, current_data: ScheduleData) -> str | None:
        """调用 LLM 生成穿搭描述，返回文本或 None"""
        persona = await self.generator._get_persona()
        outfit_negative = (self.config.get("outfit_negative", "") or "").strip()
        negative_line = f"\n穿搭禁忌（绝对不能出现）：{outfit_negative}\n" if outfit_negative else ""

        prompt = (
            f"你正在扮演以下角色：\n{persona}\n\n"
            f"换装要求：{hint}\n"
            f"当前穿搭：{current_data.outfit}\n"
            f"今日日程：{current_data.schedule}\n"
            f"{negative_line}\n"
            "请根据要求，结合角色人设和今日日程，生成新的穿搭描述。\n"
            "格式要求：\n"
            "风格：xxx\n"
            "内搭：xxx\n"
            "外装：xxx\n"
            "下装：xxx\n"
            "鞋袜：xxx\n"
            "饰品：xxx\n\n"
            "直接输出穿搭描述，不要输出JSON，不要加额外解释。"
        )

        provider = self.context.get_using_provider()
        if not provider:
            return None
        resp = await provider.text_chat(prompt, session_id="life_outfit_change")
        text = resp.completion_text.strip() if hasattr(resp, "completion_text") else str(resp).strip()
        return text or None

    @staticmethod
    def _parse_outfit_style(outfit_text: str) -> str:
        """从穿搭文本中提取风格"""
        m = re.match(r"^\s*风格[：:]\s*(.+)", outfit_text)
        return m.group(1).strip() if m else ""

    def _apply_outfit(self, data: ScheduleData, new_outfit: str) -> None:
        """将新穿搭写入数据并持久化"""
        data.outfit = new_outfit
        data.outfit_style = self._parse_outfit_style(new_outfit)
        self.data_mgr.set(data)

    # ---------- 定时换装回调 ----------

    async def _outfit_change(self, hint: str):
        """定时换装回调"""
        today = datetime.datetime.now()
        data = self.data_mgr.get(today)
        if not data:
            logger.warning("[LifeScheduler] 换装失败：今日无日程数据")
            return

        try:
            new_outfit = await self._generate_outfit(hint, data)
            if not new_outfit:
                logger.warning("[LifeScheduler] 换装失败：LLM 返回为空或无可用提供商")
                return
            self._apply_outfit(data, new_outfit)
            logger.info(f"[LifeScheduler] 定时换装完成：{data.outfit_style or '未知风格'}")
        except Exception as e:
            logger.error(f"[LifeScheduler] 定时换装失败: {e}")

    # ---------- System Prompt 注入 ----------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """System Prompt 注入"""
        data = self._get_latest_data()
        if not data:
            return

        inject_text = f"""
<character_state>
时间: {time_desc()}
穿着: {data.outfit}
日程: {data.schedule}
</character_state>
[上述状态仅供需要时参考，无需主动提及]"""

        req.system_prompt += inject_text
        logger.debug(f"[LLM] 添加的内在状态注入：{inject_text}")

    # ---------- 命令 ----------

    @filter.command("查看日程", alias={"life show"})
    async def life_show(self, event: AstrMessageEvent):
        """查看今日的日程"""
        today = datetime.datetime.now()
        today_str = today.strftime("%Y-%m-%d")

        data = self.data_mgr.get(today)
        if not data:
            try:
                yield event.plain_result("今日还没日程，正在生成...")
                data = await self.generator.generate_schedule(today, event.unified_msg_origin)
            except RuntimeError:
                yield event.plain_result("日程正在生成中，请稍后再查看")
                return
        yield event.plain_result(
            f"📅 {today_str}\n👗 今日穿搭：{data.outfit}\n📝 日程安排：\n{data.schedule}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重写日程", alias={"life renew"})
    async def life_renew(self, event: AstrMessageEvent, extra: str | None = None):
        """重写今日的日程，可附加补充要求。用法：重写日程 [补充要求]"""
        today = datetime.datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        if extra:
            yield event.plain_result(f"正在根据补充要求重写今日日程：{extra}")
        else:
            yield event.plain_result("正在重写今日日程...")
        try:
            data = await self.generator.generate_schedule(today, event.unified_msg_origin, extra=extra)
        except RuntimeError:
            yield event.plain_result("已有日程生成任务在进行中，请稍后再试")
            return
        yield event.plain_result(
            f"📅 {today_str}\n👗 今日穿搭：{data.outfit}\n📝 日程安排：{data.schedule}"
        )

    @filter.command("查穿搭", alias={"life outfit"})
    async def life_outfit_show(self, event: AstrMessageEvent):
        """查看今日穿搭"""
        today = datetime.datetime.now()
        data = self.data_mgr.get(today)
        if not data or not data.outfit:
            yield event.plain_result("今日还没有穿搭信息，请先生成日程。")
            return
        show_style = data.outfit_style and "风格" not in data.outfit
        msg = f"👗 今日穿搭\n风格：{data.outfit_style}\n{data.outfit}" if show_style else f"👗 今日穿搭\n{data.outfit}"
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("改穿搭", alias={"life outfit set"})
    async def life_outfit_set(self, event: AstrMessageEvent, extra: str | None = None):
        """修改今日穿搭。用法：改穿搭 [穿搭描述/风格要求]"""
        today = datetime.datetime.now()
        data = self.data_mgr.get(today)
        if not data:
            yield event.plain_result("今日还没有日程数据，请先使用 /查看日程 生成。")
            return
        if not extra:
            yield event.plain_result("请提供穿搭描述，例如：/改穿搭 性感风格 或 /改穿搭 换成运动装")
            return

        yield event.plain_result("正在根据你的要求生成穿搭...")

        try:
            new_outfit = await self._generate_outfit(extra, data)
            if not new_outfit:
                yield event.plain_result("LLM 返回为空或未找到可用的 LLM 提供商，穿搭未修改。")
                return
            self._apply_outfit(data, new_outfit)
            yield event.plain_result(f"👗 穿搭已更新：\n{data.outfit}")
        except Exception as e:
            logger.error(f"改穿搭失败: {e}")
            yield event.plain_result(f"生成失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日程时间", alias={"life time"})
    async def life_time(self, event: AstrMessageEvent, param: str | None = None):
        """日程时间 [HH:MM] ，设置每日日程生成时间"""
        if not param:
            yield event.plain_result("请提供时间，格式为 HH:MM，例如 /life time 07:30")
            return

        if not re.match(r"^\d{1,2}:\d{1,2}$", param):
            yield event.plain_result("时间格式错误，请使用 HH:MM 格式")
            return

        try:
            hour, minute = map(int, param.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            yield event.plain_result(
                "时间格式错误，请使用 HH:MM 格式，且小时 0-23、分钟 0-59"
            )
            return

        try:
            self.scheduler.update_schedule_time(param)
            yield event.plain_result(f"已将每日日程生成时间更新为 {param}。")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")
