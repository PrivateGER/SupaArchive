import base64
import os.path
import time
from io import BytesIO
from typing import Any

import httpx
import requests
from fastapi import FastAPI, Body
from starlette.requests import Request
from starlette.responses import StreamingResponse

import config
import model_worker
import s3
import tasks
from database import artwork_collection, translation_collection
from schema import PixivIndexPayload, PixivDownloadBatch
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.pixiv.net"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}


@app.post("/userscript/pixiv")
async def index_pixiv_artwork(pixiv_data: PixivIndexPayload):
    # Check for existence of pixiv ID
    if await artwork_collection.find_one({"pixiv_source_id": pixiv_data.illustration_id}):
        print("Already exists")
        return {"message": "This artwork already exists."}

    for idx, url in enumerate(pixiv_data.pages):
        downloadImageBatch = PixivDownloadBatch(url=url, tags=pixiv_data.tags, title=pixiv_data.title,
                                                description=pixiv_data.description,
                                                illust_id=pixiv_data.illustration_id,
                                                page_no=idx, author_id=pixiv_data.author_id,
                                                author_name=pixiv_data.author_name)
        print(downloadImageBatch.json().encode("utf-8"))
        tasks.downloadPixivImage.delay(downloadImageBatch.model_dump())

    return {"message": "The artwork has been submitted to SupaArchive."}


@app.get("/gallery")
async def gallery(request: Request, page: int = 1):
    latest_artworks = await artwork_collection.find({
        "$or": [
            {"page_no": {"$exists": False}},
            {"page_no": 0}
        ]
    }).skip((page - 1) * 25).sort("added_at", -1).limit(25).to_list(25)
    images = []
    for artwork in latest_artworks:
        images.append(
            {"url": f"{s3.base_url}{artwork['s3_object_name']}", "title": artwork["title"], "tags": artwork["tags"],
             "id": artwork["_id"],
             "vitEmbedding": artwork["vitEmbedding"] is not None and len(artwork["vitEmbedding"]) > 0})
    return templates.TemplateResponse("gallery.html", {"request": request, "images": images, "page": page})


@app.get("/administration")
async def indexing(request: Request):
    return templates.TemplateResponse("administration.html", {"request": request})


@app.get("/image/{image_id}")
async def image(request: Request, image_id: str):
    artwork = await artwork_collection.find_one({"_id": image_id})
    artwork["url"] = f"{s3.base_url}{artwork['s3_object_name']}"

    similar_artwork_task = model_worker.get_similar.delay(image_id, limit=10)
    similar_artwork_task.wait()
    similar_artworks = similar_artwork_task.get()
    for similar in similar_artworks:
        similar["url"] = f"{s3.base_url}{similar['s3_object_name']}"

    pixiv_pages_flag = False
    pixiv_other_pages = []
    # If image has a pixiv_source_id, check if there are others in the set
    if artwork["pixiv_source_id"]:
        pixiv_other_pages = await artwork_collection.find({"pixiv_source_id": artwork["pixiv_source_id"]}).sort(
            {"page_no": 1}).to_list(10)
        if len(pixiv_other_pages) > 1:
            pixiv_pages_flag = True

    # Check for translations
    artwork["translated"] = False
    translations = await translation_collection.find({"_id": image_id}).to_list(1)
    if translations:
        artwork["title"] = translations[0]["title"]
        artwork["description"] = translations[0]["description"]
        artwork["translated"] = True

    return templates.TemplateResponse("image.html", {"request": request, "image": artwork, "similar": similar_artworks,
                                                     "pixiv_pages_flag": pixiv_pages_flag,
                                                     "pixiv_set": pixiv_other_pages})


@app.get("/imgproxy/thumbnail/{image_id}")
async def imgproxy(image_id: str):
    artwork = await artwork_collection.find_one({"_id": image_id})
    encoded_url = base64.b64encode(f"{s3.base_url}{artwork['s3_object_name']}".encode("utf-8")).decode("utf-8")

    # Check if file is cached in tempdir
    # If not, fetch from imgproxy
    if os.path.exists(f"/tmp/{artwork.id}.webp"):
        return StreamingResponse(open(f"/tmp/{artwork.id}.webp", "rb"), media_type="image/webp")

    # return a streaming response
    url = httpx.URL(f"{config.IMGPROXY_THUMBNAIL_BASE_URL}{encoded_url}.webp")
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        # save to tempdir
        with open(f"/tmp/{artwork.id}.webp", "wb") as f:
            f.write(response.content)

        return StreamingResponse(BytesIO(response.content), media_type="image/webp")


@app.get("/imgproxy/optimized/{image_id}")
async def imgproxy(image_id: str):
    artwork = await artwork_collection.find_one({"_id": image_id})
    encoded_url = base64.b64encode(f"{s3.base_url}{artwork['s3_object_name']}".encode("utf-8")).decode("utf-8")

    # Check if file is cached in tempdir
    # If not, fetch from imgproxy
    if os.path.exists(f"/tmp/{artwork.id}_orig.webp"):
        return StreamingResponse(open(f"/tmp/{artwork.id}_orig.webp", "rb"), media_type="image/webp")

    # return a streaming response
    url = httpx.URL(f"{config.IMGPROXY_OPTIMIZED_BASE_URL}{encoded_url}.webp")
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        # save to tempdir
        with open(f"/tmp/{artwork.id}_orig.webp", "wb") as f:
            f.write(response.content)

        return StreamingResponse(BytesIO(response.content), media_type="image/webp")


@app.get("/search")
async def search(request: Request, query: str = None, page: int = 1, neural: bool = False, group_sets: bool = False):
    start_time = time.time()
    if query:
        if query.startswith("pixiv_id:"):
            results = await artwork_collection.find({"pixiv_source_id": int(query.split(":")[1])}).sort(
                {"page_no": 1}).to_list(100)
            return templates.TemplateResponse("search.html",
                                              {"request": request, "results": results, "page": page, "query": query,
                                               "time": time.time() - start_time, "group_sets": group_sets})
        else:
            split_tags = query.split(" ")
            results = []
            if neural:
                search_task = model_worker.neural_search.delay(query, page=page, limit=25)
                search_task.wait()
                results = search_task.get()
            else:
                search_task = model_worker.tag_search.delay(split_tags, page, 25, group_sets=group_sets)
                search_task.wait()
                results = search_task.get()

            return templates.TemplateResponse("search.html",
                                              {"request": request, "results": results, "page": page, "query": query,
                                               "neural": neural, "group_sets": group_sets,
                                               "time": time.time() - start_time})
    else:
        return templates.TemplateResponse("search.html", {"request": request})


@app.get("/stats")
async def stats(request: Request):
    total_images = await artwork_collection.count_documents({})
    total_translations = await translation_collection.count_documents({})
    total_sets = await artwork_collection.count_documents({"$or": [{"page_no": {"$exists": False}}, {"page_no": 0}]})

    top_tags = await artwork_collection.aggregate([
        {"$unwind": "$tags"},
        {"$group": {
            "_id": "$tags",
            "count": {"$sum": 1}
        }},
        {"$sort": {"count": -1}}
    ]).to_list(25)
    top_tags = [{"tag": tag["_id"], "count": tag["count"]} for tag in top_tags]

    tag_count = (await artwork_collection.aggregate([
        {"$unwind": "$tags"},
        {"$group": {
            "_id": "$tags"
        }},
        {"$group": {
            "_id": None,
            "uniqueTagCount": {"$sum": 1}
        }},
        {"$project": {
            "_id": 0,
            "uniqueTagCount": 1
        }}
    ]).to_list(1))[0]["uniqueTagCount"]

    artist_count = (await artwork_collection.aggregate([
        {"$group": {
            "_id": "$author_name"
        }},
        {"$group": {
            "_id": None,
            "uniqueArtistCount": {"$sum": 1}
        }},
        {"$project": {
            "_id": 0,
            "uniqueArtistCount": 1
        }}
    ]).to_list(1))[0]["uniqueArtistCount"]

    return templates.TemplateResponse("stats.html",
                                      {"request": request, "total_images": total_images, "total_sets": total_sets,
                                       "top_tags": top_tags, "total_translations": total_translations, "total_tags": tag_count, "total_artists": artist_count})


@app.post("/api/translate")
async def translate_artwork(
        payload: Any = Body(None),
):
    if not payload:
        return {"message": "No payload."}
    image_id = payload["image_id"]
    task = tasks.translate_image_metadata.delay(image_id)
    task.wait()

    return {"message": "Translated artwork."}


@app.post("/api/missingembeddings")
async def index_missing_images():
    model_worker.index_missing_images.delay()
    return {"message": "Indexing missing images."}


@app.post("/api/deletebroken")
async def delete_broken_art():
    tasks.delete_broken_art.delay()
    return {"message": "Deleting broken images."}


@app.post("/api/deletevideos")
async def delete_videos():
    tasks.delete_videos.delay()
    return {"message": "Deleting videos."}

@app.post("/api/clearcache")
async def clear_cache():
    for file in os.listdir("/tmp"):
        if file.endswith(".webp"):
            os.remove(f"/tmp/{file}")
    return {"message": "Cleared cache."}
