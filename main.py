import asyncio
import hashlib
from datetime import datetime

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from pathlib import Path
from typing import Any, Dict


@register("apod", "Cysheper", "NASA APOD plugin", "0.0.1")
class APOD(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        self.config = config
        self.context = context
        self.last_apod_error: str | None = None

    async def initialize(self):
        logger.info("正在初始化 NASA APOD 插件...")
        self.token = self.config.get("token", "")
        if not self.token:
            logger.warning("未找到 NASA API Token，请在插件配置中填写 `token` 字段。")

        self.image = self.config.get("image", True)
        self.explanation = self.config.get("explanation", [])
        self.title = self.config.get("title", [])
        self.provider = self.config.get("provider", "")
        self.date = self.config.get("date", [])
        self.is_divided = self.config.get("is_divided", True)
        self.timeout = self.config.get("timeout", 120)
        self.retry_count = self.config.get("retry_count", 2)

        if (
            (self.explanation.get("is_show") and self.explanation.get("is_translate"))
            or (self.title.get("is_show") and self.title.get("is_translate"))
            and not self.provider
        ) and not self.provider:
            logger.warning(
                "已启用翻译功能，但未配置语言模型提供商 ID。"
                "请在插件配置中填写 `provider` 字段。"
            )

    @filter.command("apod")
    async def apod(self, event: AstrMessageEvent):
        logger.info("正在获取 NASA 每日天文图片...")

        # 如果要翻译文本，但未配置提供商，则提示错误
        if (
            (self.explanation.get("is_show") and self.explanation.get("is_translate"))
            or (self.title.get("is_show") and self.title.get("is_translate"))
        ) and not self.provider:
            logger.warning(
                "已启用翻译功能，但未配置语言模型提供商 ID。"
                "请在插件配置中填写 `provider` 字段。"
            )
            return

        apod_data = await self.get_cache_apod()

        # 如果获取数据失败，返回错误信息（如果有的话）
        if not apod_data:
            yield event.plain_result(
                self.last_apod_error or "获取 APOD 数据失败，请稍后重试。"
            )
            return

        # 如果媒体类型不是图片，且配置了要返回图片，则提示错误
        if apod_data.get("media_type") != "image" and self.image:
            yield event.plain_result("今天的 APOD 不是图片类型，请稍后再试。")
            return

        # 提取需要的信息
        explanation = apod_data.get("explanation", None)
        title = apod_data.get("title", None)
        url = apod_data.get("hdurl", apod_data.get("url", None))
        date = apod_data.get("date", None)

        # 如果不存在图片链接，且要返回图片，则提示错误
        if not url and self.image:
            yield event.plain_result("获取 APOD 图片链接失败，请稍后重试。")
            return

        # 翻译文本（如果需要）
        # 从缓存中获取翻译结果，以避免重复翻译同一文本造成不必要的 API 调用和错误日志
        explanation_zh, title_zh = None, None
        try:
            # 只有在需要翻译且提供了语言模型提供商的情况下才进行翻译，以避免不必要的错误日志
            if (
                self.explanation.get("is_show")
                and self.explanation.get("is_translate")
                and self.provider
                and explanation
            ):
                if translate_cache := await self.get_cache(explanation):
                    explanation_zh = translate_cache
                else:
                    explanation_zh = await self.translate_explanation(
                        explanation.strip(), self.provider
                    )
                    await self.put_cache(explanation, explanation_zh)

            if (
                self.title.get("is_show")
                and self.title.get("is_translate")
                and self.provider
                and title
            ):
                if translate_cache := await self.get_cache(title):
                    title_zh = translate_cache
                else:
                    title_zh = await self.translate_explanation(
                        title.strip(), self.provider
                    )
                    await self.put_cache(title, title_zh)
        except Exception as exc:
            logger.error(f"翻译 APOD 文本失败：{exc}")

        # 分段返回
        if self.is_divided:
            if self.image and url:
                yield event.image_result(url)
            if self.title.get("is_show"):
                yield event.plain_result(f"标题：{title_zh if title_zh else title}")
            if self.date.get("is_show"):
                yield event.plain_result(f"日期：{date}")
            if self.explanation.get("is_show") and (explanation_zh or explanation):
                yield event.plain_result((explanation_zh or explanation or "").strip())
            return

        # 连接返回
        chain = []
        if self.image and url:
            chain.append(Comp.Image.fromURL(url))
        if self.title.get("is_show"):
            chain.append(Comp.Plain(f"标题：{title_zh if title_zh else title} \n"))
        if self.date.get("is_show"):
            chain.append(Comp.Plain(f"日期：{date} \n"))
        if self.explanation.get("is_show") and (explanation_zh or explanation):
            chain.append(Comp.Plain((explanation_zh or explanation or "").strip()))
        yield event.chain_result(chain)

    async def put_cache(self, key: Any, value: Any):
        """将结果存入缓存。"""
        logger.info(f"正在缓存结果")
        try:
            await self.put_kv_data(key, value)
        except Exception as exc:
            logger.error(f"缓存结果失败：{exc}")

    async def get_cache(self, key: str) -> Any | None:
        """从缓存中获取缓存结果。如果缓存中没有对应的结果，返回 None。"""
        logger.info(f"正在从缓存中获取缓存结果")
        try:
            data = await self.get_kv_data(key, None)
            logger.info(f"已从缓存中获取缓存结果")
            return data
        except Exception as exc:
            logger.error(f"获取缓存结果失败：{exc}")
            return None

    async def translate_explanation(self, explanation: str, provider_id: str) -> str:
        logger.info("正在翻译 APOD 说明文本...")
        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=(
                "You are a professional astronomy translator. Translate the input text into "
                "Simplified Chinese accurately, and do not add any extra explanation."
            ),
            prompt=explanation,
        )
        return llm_resp.completion_text

    async def get_cache_apod(self) -> dict | None:
        # 首先尝试从缓存中获取 APOD 数据，以避免不必要的 API 调用和潜在的错误日志。如果缓存数据无效或过期，再获取最新数据。
        apod_data = await self.get_cache("apod_cache")
        is_use_cache = True

        # 断言缓存中的数据是一个字典，并且包含必要的字段，以避免使用无效的缓存数据导致后续错误
        if (
            not isinstance(apod_data, dict)
            or "date" not in apod_data
            or "explanation" not in apod_data
        ):
            logger.error(f"获取到的 APOD 数据无效：{apod_data}")
            is_use_cache = False

        # 检查缓存数据是否过期。APOD 数据每天更新一次，如果缓存中的数据不是今天的，或者已经超过12小时，则认为过期。
        time = datetime.now()

        # 如果缓存数据存在且有效，且未过期，则使用缓存数据；否则获取最新数据
        if apod_data and "retrieved_at" in apod_data:
            retrieved_at = datetime.fromisoformat(apod_data["retrieved_at"])
            if (
                (time - retrieved_at).total_seconds() > 12 * 3600
                or retrieved_at.date() != time.date()
            ):  # 如果缓存数据超过12小时，则认为过期 或者者日期不是今天的日期（以防用户在一天内多次请求导致缓存数据过期但未被更新）
                logger.info("缓存中的 APOD 数据已过期，正在获取最新数据...")
                apod_data = None
                is_use_cache = False

        # 如果没有有效的缓存数据，或者用户请求不使用缓存，则获取最新数据，并将其存入缓存以供后续使用
        if not apod_data or not is_use_cache:
            logger.info("未从缓存中获取到 APOD 数据，正在获取最新数据...")
            apod_data = await self.get_apod()

            # 断言获取到的数据是一个字典，并且包含必要的字段，以避免缓存无效数据
            if (
                not isinstance(apod_data, dict)
                or "date" not in apod_data
                or "explanation" not in apod_data
            ):
                logger.error(f"获取到的 APOD 数据无效：{apod_data}")
                return None
            apod_data["retrieved_at"] = (
                datetime.now().isoformat()
            )  # 添加一个字段来记录数据的获取时间，超出一定时间后可以认为缓存过期
            await self.put_cache("apod_cache", apod_data)

        return apod_data

    async def get_apod(self) -> dict | None:
        base_url = f"https://api.nasa.gov/planetary/apod?api_key={self.token}"
        retryable_statuses = {502, 503, 504}
        self.last_apod_error = None
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(self.retry_count + 1):
                try:
                    async with session.get(base_url) as response:
                        if response.status >= 400:
                            error_text = await response.text()
                            logger.error(
                                f"获取 APOD 数据失败：状态码={response.status}，响应正文={error_text}"
                            )

                            if response.status == 429:
                                self.last_apod_error = (
                                    "NASA API 已达到速率限制，请稍后再试。"
                                )
                            elif response.status in retryable_statuses:
                                self.last_apod_error = (
                                    "NASA APOD 服务暂时不可用，请稍后重试。"
                                )
                                if attempt < self.retry_count:
                                    await asyncio.sleep(min(2**attempt, 4))
                                    continue
                            elif response.status in {401, 403}:
                                self.last_apod_error = "NASA API Token 无效或未授权。"
                            else:
                                self.last_apod_error = f"从 NASA 获取 APOD 数据失败（HTTP {response.status}）。"
                            return None

                        self.last_apod_error = None
                        return await response.json()
                except asyncio.TimeoutError:
                    logger.error(f"获取 APOD 数据超时：请求在 {self.timeout} 秒后超时")
                    self.last_apod_error = "请求 NASA APOD 超时，请稍后重试。"
                    if attempt < self.retry_count:
                        await asyncio.sleep(min(2**attempt, 4))
                        continue
                    return None
                except aiohttp.ClientError as exc:
                    logger.error(f"获取 APOD 数据失败：客户端错误：{exc}")
                    self.last_apod_error = "连接 NASA APOD 时发生网络错误，请稍后重试。"
                    if attempt < self.retry_count:
                        await asyncio.sleep(min(2**attempt, 4))
                        continue
                    return None
                except Exception as exc:
                    logger.error(f"获取 APOD 数据失败：未知异常：{exc}")
                    self.last_apod_error = "获取 APOD 数据时发生未知错误。"
                    return None
                finally:
                    # 只有在没有错误的情况下才记录尝试完成的日志，以避免日志被过多的错误信息淹没
                    if not self.last_apod_error:
                        logger.info(
                            f"获取 APOD 数据尝试 {attempt + 1}/{self.retry_count + 1} 完成"
                        )

        return None

    async def terminate(self):
        # 在插件被卸载或 AstrBot 关闭时执行清理操作（如果需要）
        logger.info("正在终止 NASA APOD 插件...")
        pass
