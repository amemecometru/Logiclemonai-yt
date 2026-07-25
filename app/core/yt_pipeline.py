import asyncio
import time
import json
import os
import uuid
import re
import httpx
from urllib.parse import quote
from typing import Dict, Any, Optional, List

from app.services.cf_ai_service import run_cf_ai
from app.models.content import ContentStatus
from app.services.database_service import DatabaseService


class YTPipeline:
    def __init__(self):
        self.active_tasks: Dict[str, Dict[str, Any]] = {}
        self.db = DatabaseService()

    def register_task(self) -> str:
        """Create a task entry up front (status=processing) and return its id."""
        task_id = f"yt_{uuid.uuid4().hex[:12]}"
        self.active_tasks[task_id] = {
            "status": ContentStatus.PROCESSING,
            "start_time": time.time(),
            "current_agent": None,
            "progress": 0
        }
        self._evict_old_tasks()
        return task_id

    async def run_full_studio_pipeline(
        self, 
        topic: str, 
        product_details: str = "", 
        task_id: Optional[str] = None,
        image_tier: str = "standard"
    ) -> Dict[str, Any]:
        """Consolidated 2-pass studio pipeline: Single-pass LLM + Parallel Image Engine."""
        if task_id is None:
            task_id = self.register_task()
        elif task_id not in self.active_tasks:
            self.active_tasks[task_id] = {
                "status": ContentStatus.PROCESSING,
                "start_time": time.time(),
                "current_agent": None,
                "progress": 0
            }

        try:
            # STEP 1: SINGLE-PASS AI GENERATION
            self.active_tasks[task_id]["current_agent"] = "ai_studio"
            self.active_tasks[task_id]["progress"] = 20

            script_prompt = f"""Create a complete YouTube Script, a 4-post X (Twitter) thread, and 4 16:9 cinematic visual scene prompts for topic: '{topic}'. Context/Product: '{product_details}'.

Return strictly valid JSON with this exact schema:
{{
  "script": {{
    "title": "Catchy Video Title",
    "description": "Engaging description with hashtags",
    "tags": "tag1, tag2, tag3",
    "intro": "Hook and introduction...",
    "body": "Main content body...",
    "outro": "Outro and CTA..."
  }},
  "x_thread": ["Post 1...", "Post 2...", "Post 3...", "Post 4..."],
  "visual_prompts": ["Prompt 1...", "Prompt 2...", "Prompt 3...", "Prompt 4..."]
}}"""

            text_response = await run_cf_ai(script_prompt)
            clean_json = text_response.strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            
            content = json.loads(clean_json.strip())
            self.active_tasks[task_id]["progress"] = 60

            # STEP 2: PARALLEL IMAGE GENERATION
            self.active_tasks[task_id]["current_agent"] = f"renderer_{image_tier}"
            prompts = content.get("visual_prompts", [])
            
            if not prompts:
                prompts = [f"Hyper-realistic 8k 16:9 scene for: {p}" for p in content.get("x_thread", [])[:4]]

            async def render_single_image(idx: int, prompt_text: str) -> Dict[str, Any]:
                encoded = quote(prompt_text)
                if image_tier == "ultra":
                    img_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1920&height=1080&model=flux&nologo=true&enhance=true"
                else:
                    img_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&nologo=true"
                
                return {
                    "scene_number": idx + 1,
                    "action": f"Scene {idx + 1}",
                    "prompt": prompt_text,
                    "image_url": img_url,
                    "tier": image_tier
                }

            render_tasks = [render_single_image(i, p) for i, p in enumerate(prompts[:4])]
            rendered_images = await asyncio.gather(*render_tasks)

            self.active_tasks[task_id]["progress"] = 90

            # STEP 3: PACKAGE FINAL PAYLOAD
            execution_time = time.time() - self.active_tasks[task_id]["start_time"]
            result = {
                "task_id": task_id,
                "status": "success",
                "topic": topic,
                "image_tier": image_tier,
                "execution_time": round(execution_time, 2),
                "script": content.get("script", {}),
                "x_thread": content.get("x_thread", []),
                "thumbnail_url": rendered_images[0]["image_url"] if rendered_images else "",
                "x_visual_pack": rendered_images
            }

            self.active_tasks[task_id]["status"] = ContentStatus.COMPLETED
            self.active_tasks[task_id]["progress"] = 100
            self.active_tasks[task_id]["result"] = result
            await self._persist_result(task_id, result)
            self._evict_old_tasks()

            return result

        except Exception as e:
            return self._error_response(task_id, "Full Studio Pipeline failed", str(e))

    async def get_task_status(self, task_id: str) -> Dict[str, Any]:
        if task_id not in self.active_tasks:
            persisted = await self.db.get_yt_video(task_id)
            if persisted:
                return {
                    "task_id": task_id,
                    "status": persisted.get("status", "completed"),
                    "progress": 100,
                    "current_agent": None,
                    "elapsed_time": persisted.get("execution_time", 0),
                    "result": persisted.get("result", {})
                }
            return {"error": "Task not found"}
        task = self.active_tasks[task_id]
        status = {
            "task_id": task_id,
            "status": task["status"],
            "progress": task["progress"],
            "current_agent": task.get("current_agent"),
            "elapsed_time": round(time.time() - task["start_time"], 2)
        }
        if task.get("result") is not None:
            status["result"] = task["result"]
        return status

    def _evict_old_tasks(self, max_tasks: int = 200):
        if len(self.active_tasks) <= max_tasks:
            return
        oldest = sorted(self.active_tasks, key=lambda k: self.active_tasks[k].get("start_time", 0))
        for tid in oldest[: len(self.active_tasks) - max_tasks]:
            self.active_tasks.pop(tid, None)

    async def _persist_result(self, task_id: str, result: Dict[str, Any]):
        try:
            script = result.get("script", {}) or {}
            await self.db.save_yt_video({
                "id": task_id,
                "topic": result.get("topic"),
                "title": script.get("title") or result.get("topic"),
                "status": "completed",
                "result": result,
                "execution_time": result.get("execution_time", 0),
            })
        except Exception as e:
            print(f"[YT] persist failed for {task_id}: {e}")

    def _error_response(self, task_id: str, error_type: str, message: str) -> Dict[str, Any]:
        resp = {
            "status": "error",
            "task_id": task_id,
            "error_type": error_type,
            "message": message
        }
        if task_id in self.active_tasks:
            self.active_tasks[task_id]["status"] = ContentStatus.FAILED
            self.active_tasks[task_id]["result"] = resp
        return resp
