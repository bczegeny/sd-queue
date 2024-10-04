from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import gradio as gr
import logging
import base64
import os
from datetime import datetime

from modules.api import api, models
from modules import script_callbacks
import modules.shared as shared
from secrets import compare_digest

from scripts.task_manager import TaskManager

import requests

version = "0.0.1"

task_manager = TaskManager()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def async_api(_: gr.Blocks, app: FastAPI):
    if shared.cmd_opts.api_auth:
        opts_credentials = {}
        for auth in shared.cmd_opts.api_auth.split(","):
            user, password = auth.split(":")
            opts_credentials[user] = password

    def auth(credentials: HTTPBasicCredentials = Depends(HTTPBasic())):
        if credentials.username in opts_credentials:
            if compare_digest(credentials.password, opts_credentials[credentials.username]):
                return True

        raise HTTPException(status_code=401, detail="Incorrect username or password", headers={"WWW-Authenticate": "Basic"})

    def auth_required():
        return shared.cmd_opts.api_auth is not False and shared.cmd_opts.api_auth is not None

    def get_auth_dependency():
        if auth_required():
            return [Depends(auth)]
        return []

    @app.get("/sd-queue/login", dependencies=get_auth_dependency())
    async def login():
        return {"status": True, "version": version}

    @app.post("/sd-queue/txt2img", dependencies=get_auth_dependency())
    async def txt2imgapi(request: Request, txt2imgreq: models.StableDiffusionTxt2ImgProcessingAPI):
        route = next((route for route in request.app.routes if route.path == "/sdapi/v1/txt2img"), None)
        if route:
            task_id, success = task_manager.add_task(route.endpoint, txt2imgreq)
            if success:
                return {"status": "queued", "task_id": task_id}
            else:
                raise HTTPException(status_code=503, detail="Queue is full")
        return {"status": "error"}

    @app.get("/sd-queue/{task_id}/status", dependencies=get_auth_dependency())
    async def get_task_status(task_id: str, request: Request):
        logger.info(f"Retrieving status for task {task_id}")
        task = task_manager.get_status(task_id)
        if not task:
            logger.warning(f"Task {task_id} not found")
            raise HTTPException(status_code=404, detail="Task not found")

        response = {"status": task["status"]}
        logger.info(f"Task {task_id} status: {task['status']}")

        if "queue_position" in task:
            response["queue_position"] = task["queue_position"]

        if task["status"] == "completed":
            logger.info(f"Task {task_id} completed. Processing result.")
            if "result" in task:
                response["result"] = task["result"]
                logger.info(f"Result keys for task {task_id}: {task['result'].keys()}")
                if "images" in task["result"]:
                    logger.info(f"Images found in result for task {task_id}. Number of images: {len(task['result']['images'])}")
                    if isinstance(task["result"]["images"], list) and len(task["result"]["images"]) > 0:
                        # Assuming the first image is the main one
                        image_data = task["result"]["images"][0]
                        logger.info(f"Image data length for task {task_id}: {len(image_data)}")

                        # Create the directory if it doesn't exist
                        today = datetime.now().strftime("%Y-%m-%d")
                        save_dir = os.path.join("outputs", "txt2img-images", today)
                        os.makedirs(save_dir, exist_ok=True)

                        # Save the image
                        image_filename = f"{task_id}.png"
                        image_path = os.path.join(save_dir, image_filename)

                        try:
                            with open(image_path, "wb") as image_file:
                                image_file.write(base64.b64decode(image_data))

                            logger.info(f"Image saved for task {task_id}: {image_path}")

                            # Construct the full URL to the image
                            relative_path = os.path.join("outputs", "txt2img-images", today, image_filename)
                            image_url = f"{request.base_url}file={relative_path}"
                            response["image_url"] = image_url
                            logger.info(f"Image URL for task {task_id}: {image_url}")
                        except Exception as e:
                            logger.error(f"Error saving image for task {task_id}: {str(e)}")
                else:
                    logger.warning(f"Images list is empty for task {task_id}")
            else:
                logger.warning(f"No 'images' key found in result for task {task_id}")
        else:
            logger.warning(f"No 'result' key found for completed task {task_id}")
        elif task["status"] == "in-progress":
            route = next((route for route in request.app.routes if route.path == "/sdapi/v1/progress"), None)
            if route:
                progressreq = models.ProgressRequest(skip_current_image=False)
                info = route.endpoint(progressreq)
                response["progress"] = info.progress
                response["eta_relative"] = info.eta_relative
            else:
                logger.warning("Route /sdapi/v1/progress not found")

        logger.info(f"Final response for task {task_id}: {response}")
        return response

    @app.delete("/sd-queue/{task_id}/remove", dependencies=get_auth_dependency())
    async def remove_specific_task(task_id: str):
        if task_manager.remove_specific_task(task_id):
            return {"status": "success", "message": f"Task {task_id} has been removed"}
        else:
            raise HTTPException(status_code=400, detail="Task not found or cannot be removed because it's in progress")

script_callbacks.on_app_started(async_api)
