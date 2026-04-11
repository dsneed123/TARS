"""TARS image handler — download Discord attachments and create website integration tasks."""

import hashlib
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("tars.image_handler")

TARS_HOME = Path(os.environ.get("TARS_HOME", Path(__file__).parent.parent))
REPOS_DIR = TARS_HOME / "repos"
CONFIG_DIR = TARS_HOME / "config"
QUEUES_DIR = CONFIG_DIR / "queues"

# Supported image types
SUPPORTED_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB


class ImageHandler:
    """Downloads Discord images and saves them to a project repo."""

    def __init__(self, project_name: str):
        self.project_name = project_name
        self.repo_dir = REPOS_DIR / project_name
        self.images_dir = self.repo_dir / "public" / "images"

    async def download_attachment(
        self,
        attachment,  # discord.Attachment object
    ) -> Optional[dict]:
        """Download a Discord attachment and save to the repo.

        Args:
            attachment: discord.Attachment object with .url, .filename, .size, .content_type

        Returns dict with: local_path, web_path, filename, size_bytes, media_type
        Returns None if download fails or file type is unsupported.
        """
        filename = attachment.filename
        ext = Path(filename).suffix.lower()

        if ext not in SUPPORTED_EXTENSIONS:
            logger.warning("Unsupported image type: %s", ext)
            return None

        if attachment.size > MAX_IMAGE_SIZE:
            logger.warning("Image too large: %d bytes (max %d)", attachment.size, MAX_IMAGE_SIZE)
            return None

        # Sanitize and prefix filename with timestamp
        safe_name = self._sanitize_filename(filename)

        # Ensure images directory exists
        self.images_dir.mkdir(parents=True, exist_ok=True)

        local_path = self.images_dir / safe_name

        try:
            # discord.py Attachment.save() handles the download
            await attachment.save(local_path)
        except Exception as e:
            logger.error("Failed to download image %s: %s", filename, e)
            return None

        media_type = SUPPORTED_EXTENSIONS.get(ext, attachment.content_type or "image/unknown")

        info = {
            "local_path": str(local_path),
            "web_path": f"/images/{safe_name}",
            "filename": safe_name,
            "original_filename": filename,
            "size_bytes": attachment.size,
            "media_type": media_type,
            "saved_at": datetime.now().isoformat(),
        }

        logger.info("Saved image: %s -> %s", filename, local_path)
        return info

    def create_integration_task(
        self,
        image_info: dict,
        user_description: str = "",
        username: str = "User",
    ) -> dict:
        """Create a TARS task to integrate the image into the website.

        Appends to the project's task queue.
        Returns the task dict.
        """
        QUEUES_DIR.mkdir(parents=True, exist_ok=True)
        queue_path = QUEUES_DIR / f"{self.project_name}.yaml"

        if queue_path.exists():
            with open(queue_path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        tasks = data.setdefault("tasks", [])

        task_hash = hashlib.md5(
            f"{self.project_name}{image_info['filename']}{datetime.now().isoformat()}".encode()
        ).hexdigest()[:8]

        filename = image_info["filename"]
        web_path = image_info["web_path"]

        description_parts = [
            f"Add image `{filename}` to the website.",
            f"Image is already saved at `public/images/{filename}`.",
            f"Web path: `{web_path}`",
        ]
        if user_description:
            description_parts.append(f"\nUser context: {user_description}")
        description_parts.append(
            "\nIntegrate this image into the appropriate page. "
            "Add proper alt text, lazy loading, and responsive sizing."
        )

        task = {
            "id": f"img-{task_hash}",
            "title": f"Add image {filename} to website",
            "description": "\n".join(description_parts),
            "project": self.project_name,
            "priority": 120,
            "status": "pending",
            "source": "discord-image",
            "created": datetime.now().isoformat(),
        }
        tasks.append(task)

        with open(queue_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        logger.info("Created image task: %s for %s", task["id"], self.project_name)
        return task

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename: lowercase, replace spaces, add timestamp prefix."""
        name = Path(filename)
        stem = name.stem.lower()
        ext = name.suffix.lower()

        # Replace non-alphanumeric chars with hyphens
        stem = re.sub(r"[^a-z0-9]", "-", stem)
        stem = re.sub(r"-+", "-", stem).strip("-")

        # Prefix with timestamp for uniqueness
        timestamp = int(time.time())
        return f"{timestamp}-{stem}{ext}"
