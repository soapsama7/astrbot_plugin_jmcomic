import re
import asyncio
import img2pdf
import jmcomic
import shutil
from pathlib import Path
from datetime import datetime
from PIL import Image

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


def _parse_whitelist(raw: list | dict) -> set[str]:
    """Parse whitelist: supports list ['id', 'id:name'] or dict {'id': 'name'}."""
    allowed = set()
    if isinstance(raw, dict):
        allowed = {str(k).strip() for k in raw.keys() if str(k).strip()}
    elif isinstance(raw, list):
        for item in raw:
            s = str(item).strip()
            if not s:
                continue
            # Format: "123456789" or "123456789:我的群"
            gid = s.split(":", 1)[0].strip() if ":" in s else s
            if gid:
                allowed.add(gid)
    return allowed


def _is_allowed_group(group_id: str | None, config: object) -> bool:
    """Check if session is allowed by config (group whitelist + private toggle)."""
    if config is None or not hasattr(config, "get"):
        return True

    allow_private = config.get("allow_private", True)
    if not group_id:
        return bool(allow_private)

    raw = config.get("whitelist_groups", [])
    if not raw:
        return True

    allowed = _parse_whitelist(raw)
    return str(group_id) in allowed


@register("jmcomic", "Developer", "JM 漫画下载插件", "3.1.0")
class JMPlugin(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config

        self.base_dir = Path(__file__).parent
        self.cache_root = self.base_dir / "cache"
        self.cache_root.mkdir(parents=True, exist_ok=True)

    @filter.regex(r"^JM\s+(\d+)$", flags=re.IGNORECASE)
    async def jm_download(self, event: AstrMessageEvent):

        match = re.match(r"^JM\s+(\d+)$", event.message_str.strip(), re.IGNORECASE)
        if not match:
            return

        # Permission check: group whitelist & private/private toggle
        group_id = event.get_group_id()
        if not _is_allowed_group(str(group_id) if group_id else None, self.config):
            return

        comic_id = match.group(1)

        await self._try_react_received(event)

        session_id = f"{comic_id}_{datetime.now().strftime('%H%M%S%f')}"
        session_dir = self.cache_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_event_loop()

        try:
            # 下载
            await loop.run_in_executor(
                None,
                lambda: self._download_logic(comic_id, session_dir)
            )

            images = self._collect_images(session_dir)

            if not images:
                yield event.plain_result("下载失败：未找到图片")
                return

            pdf_path = session_dir / f"JM_{comic_id}.pdf"

            # 生成 PDF
            await loop.run_in_executor(
                None,
                lambda: self._create_pdf(images, pdf_path)
            )

            size_mb = pdf_path.stat().st_size / 1024 / 1024
            logger.info(f"PDF 生成完成: {pdf_path} ({size_mb:.2f} MB)")

            abs_path = str(pdf_path.resolve())

            group_id = event.get_group_id()
            if group_id and str(group_id).strip():
                await event.bot.call_action(
                    "upload_group_file",
                    group_id=int(group_id),
                    file=abs_path,
                    name=f"JM_{comic_id}.pdf"
                )
            else:
                await event.bot.call_action(
                    "upload_private_file",
                    user_id=int(event.get_sender_id()),
                    file=abs_path,
                    name=f"JM_{comic_id}.pdf"
                )

            # ===== 上传成功后清除缓存 =====
            try:
                shutil.rmtree(session_dir, ignore_errors=True)
                logger.info(f"缓存已清理: {session_dir}")
            except Exception as e:
                logger.warning(f"缓存清理失败: {e}")

        except Exception as e:
            logger.error(f"JMComic 插件异常: {str(e)}", exc_info=True)
            yield event.plain_result(f"运行出错: {str(e)}")

    async def _try_react_received(self, event: AstrMessageEvent):
        """React to the trigger message as an acknowledgement."""
        message_id = None

        raw_obj = getattr(event, "message_obj", None)
        raw_message = getattr(raw_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            message_id = raw_message.get("message_id")

        if message_id is None:
            message_id = getattr(event, "message_id", None)

        if message_id is None:
            return

        try:
            await event.bot.call_action(
                "set_msg_emoji_like",
                message_id=int(message_id),
                emoji_id=289,
                emoji_type="1",
                set=True,
            )
        except Exception:
            # Ignore reaction failures, do not block download flow.
            pass

    def _download_logic(self, comic_id: str, target_dir: Path):

        option = jmcomic.JmOption.default()
        option.dir_rule.base_dir = str(target_dir)

        jmcomic.download_album(comic_id, option=option)

    def _collect_images(self, root: Path):

        image_ext = {".jpg", ".jpeg", ".png", ".webp"}
        images = []

        for p in root.rglob("*"):
            if p.suffix.lower() in image_ext:
                images.append(p)

        def natural_key(path):
            return [
                int(t) if t.isdigit() else t
                for t in re.split(r"(\d+)", path.name)
            ]

        images.sort(key=natural_key)
        return images

    def _create_pdf(self, image_paths, pdf_output_path: Path):

        converted = []

        for img_path in image_paths:
            try:
                if img_path.suffix.lower() == ".webp":
                    with Image.open(img_path) as im:
                        rgb = im.convert("RGB")
                        new_path = img_path.with_suffix(".jpg")
                        rgb.save(new_path, "JPEG")
                        converted.append(str(new_path))
                else:
                    converted.append(str(img_path))
            except Exception as e:
                logger.warning(f"图片转换失败: {img_path} - {e}")

        if not converted:
            raise Exception("没有可用于生成 PDF 的图片")

        with open(pdf_output_path, "wb") as f:
            f.write(img2pdf.convert(converted))