import hashlib
import hashlib
import os.path
import tempfile
from time import sleep

import celery
import deepl
import requests
from celery import Celery
from pygelbooru import Gelbooru
from pymongo import MongoClient

import config
import model_worker
import s3
from async_task import async_task
from database import CONNECTION_STRING, Artwork
from s3 import upload_file
from schema import PixivDownloadBatch
from kombu import Exchange, Queue

app = Celery('tasks', broker=config.CELERY_RABBITMQ_URL, backend=config.REDIS_URL, result_expires=60 * 60 * 24)
app.conf.task_default_queue = 'tasks'
app.conf.task_queues = [
    Queue('tasks', routing_key='tasks.#', queue_arguments={'x-max-priority': 10, 'celeryd_prefetch_multiplier': 1}, max_priority=10),
]
app.conf.update(
    worker_prefetch_multiplier=1
)

gelbooru = Gelbooru()

@app.task
def downloadPixivImage(pixivImage: PixivDownloadBatch):
    pixivImage = PixivDownloadBatch.parse_obj(pixivImage)

    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")

        # Check for existence of pixiv ID
        if artwork_collection.find_one({"pixiv_source_id": pixivImage.illust_id, "page_no": pixivImage.page_no}):
            print("Already exists")
            return

        # Create temp file
        # Download image
        with tempfile.TemporaryFile() as temp:
            # Request image
            res = requests.get(pixivImage.url, headers={"Referer": "https://www.pixiv.net",
                                                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0"})

            temp.write(res.content)

            # Get sha256 of file
            temp.seek(0)
            sha256 = hashlib.sha256(temp.read()).hexdigest()
            print(sha256)

            temp.seek(0)

            artwork = Artwork(s3_object_name="placeholder", _id=sha256, tags=pixivImage.tags,
                              pixiv_source_id=pixivImage.illust_id, page_no=pixivImage.page_no, title=pixivImage.title,
                              description=pixivImage.description, pixiv_author_id=pixivImage.author_id, author_name=pixivImage.author_name)

            # Check for existence of sha256
            if artwork_collection.find_one({"_id": sha256}):
                print("Already exists, merging tags")

                artwork_collection.update_one({"_id": sha256}, {"$set": {"tags": list(set(artwork.tags + artwork_collection.find_one({"_id": sha256})["tags"]))}})
                # use $addFields to add metadata to the document
                artwork_collection.update_one({"_id": sha256}, [
                    {
                        "$set": {
                            "pixiv_source_id": {
                                "$cond": {
                                    "if": {"$eq": [pixivImage.illust_id, {"$ifNull": ["$pixiv_source_id", 0]}]},
                                    "then": "$pixiv_source_id",
                                    "else": pixivImage.illust_id
                                }
                            },
                            "page_no": {
                                "$cond": {
                                    "if": {"$eq": [pixivImage.page_no, {"$ifNull": ["$page_no", 0]}]},
                                    "then": "$page_no",
                                    "else": pixivImage.page_no
                                }
                            }
                        }
                    }
                ])
            else:
                artwork.s3_object_name = upload_file(temp, sha256, os.path.splitext(pixivImage.url)[1][1:])
                artwork_collection.insert_one(artwork.model_dump(by_alias=True))
                model_worker.generate_embeddings.apply_async([sha256], countdown=10)


@app.task
def downloadGelbooru(gelbooruImage):
    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")

        # Check for existence of gelbooru file ID
        if artwork_collection.find_one({"gelbooru_id": gelbooruImage["id"]}):
            print("Already exists")
            return

        # Check if webm or mp4
        if gelbooruImage["url"].endswith(".webm") or gelbooruImage["url"].endswith(".mp4"):
            print("Ignoring webm/mp4")
            return

        # Create temp file
        # Download image
        with tempfile.TemporaryFile() as temp:
            res = requests.get(gelbooruImage["url"])
            temp.write(res.content)
            temp.seek(0)

            # Get sha256 of file
            sha256 = hashlib.sha256(temp.read()).hexdigest()
            print(sha256)

            temp.seek(0)

            artwork = Artwork(s3_object_name="placeholder", _id=sha256, tags=gelbooruImage["tags"], gelbooru_id=gelbooruImage["id"])

            # Check for existence of sha256
            if artwork_collection.find_one({"_id": sha256}):
                print("Already exists, merging tags")

                artwork_collection.update_one({"_id": sha256}, {"$set": {"tags": list(set(artwork.tags + artwork_collection.find_one({"_id": sha256})["tags"]))}})
                # use $addFields to add metadata to the document
                artwork_collection.update_one({"_id": sha256}, {"$addFields": {"gelbooru_id": gelbooruImage["id"]}})
            else:
                artwork.s3_object_name = upload_file(temp, sha256, os.path.splitext(gelbooruImage["url"])[1][1:])
                artwork_collection.insert_one(artwork.model_dump(by_alias=True))
                model_worker.generate_embeddings.apply_async([sha256], countdown=10)


@async_task(app, bind=True)
async def fetch_gelbooru(self: celery.Task, tag, limit):
    if isinstance(tag, str):
        tag = tag.split(" ")
    print(tag)
    print(limit)
    res = await gelbooru.search_posts(tags=tag, limit=limit)
    print(res)

    for image in res:
        downloadGelbooru.delay({
            "tags": image.tags,
            "url": image.file_url,
            "source": "gelbooru",
            "id": image.id
        })


@app.task
def delete_broken_art():
    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")

        artworks = artwork_collection.find()

        for artwork in artworks:
            # Make HEAD request to see if the file exists
            res = requests.head(f"{s3.base_url}{artwork['s3_object_name']}")
            if res.status_code != 200:
                print(f"Broken link: {artwork['_id']}")
                print(f"Deleting {artwork['_id']}")
                artwork_collection.delete_one({"_id": artwork["_id"]})
                continue
            elif int(res.headers["Content-Length"]) < 2000:
                print(f"Too small: {artwork['_id']}")
                print(f"Deleting {artwork['_id']}")
                artwork_collection.delete_one({"_id": artwork["_id"]})
                continue


@app.task
def delete_videos():
    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")

        artworks = artwork_collection.find()

        for artwork in artworks:
            if artwork["s3_object_name"].endswith(".webm") or artwork["s3_object_name"].endswith(".mp4"):
                print(f"Deleting video: {artwork['_id']}")
                artwork_collection.delete_one({"_id": artwork["_id"]})
                continue


@app.task
def translate_image_metadata(image_id):
    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")

        artwork = artwork_collection.find({"_id": image_id}).limit(1)

        if not artwork:
            raise ValueError("Artwork not found")

        artwork = artwork[0]

        translator = deepl.Translator(config.DEEPL_API_KEY)

        translated_title = translator.translate_text(artwork["title"], target_lang="EN-US").text
        translated_description = translator.translate_text(artwork["description"], target_lang="EN-US").text

        translation_collection = db.get_collection("translations")

        # In case of a pixiv ID, the entire set will have the same translation.
        if artwork["pixiv_source_id"]:
            translation_collection.insert_many([
                {
                    "_id": image["_id"],
                    "title": translated_title,
                    "description": translated_description
                } for image in artwork_collection.find({"pixiv_source_id": artwork["pixiv_source_id"]})
            ])
        else:
            translation_collection.insert_one({
                "_id": image_id,
                "title": translated_title,
                "description": translated_description
            })
