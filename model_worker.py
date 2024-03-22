import hashlib
import tempfile
import uuid
from threading import Lock

import celery
import requests
from celery import Celery
from pymongo import MongoClient
from qdrant_client import QdrantClient
from triton._C.libtriton.triton.ir import value

import config
import s3
from database import CONNECTION_STRING, Artwork

import torch
from PIL import Image
import open_clip
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchText, IsNullCondition, MatchValue, PayloadField
from kombu import Exchange, Queue


app = Celery('model_worker', broker=config.CELERY_RABBITMQ_URL, backend=config.REDIS_URL, result_expires=60 * 60 * 24)
app.conf.task_default_queue = 'model_worker'
app.conf.task_queues = [
    Queue('model_worker', routing_key='model_tasks.#', queue_arguments={'x-max-priority': 10}, max_priority=10),
]
app.conf.update(
    worker_prefetch_multiplier=1
)

model, preprocess, tokenizer, qdrant_client = None, None, None, None
loadingMutex = Lock()

def load_model():
    global model, preprocess, tokenizer
    model, _, preprocess = open_clip.create_model_and_transforms(config.VIT_MODEL_NAME, pretrained=config.VIT_MODEL_PRETRAINED)
    tokenizer = open_clip.get_tokenizer('ViT-B-32')
    model.to(config.VIT_DEVICE)
    print("Model loaded")


def get_model_instances():
    with loadingMutex:
        if model is None:
            load_model()
        return model, preprocess, tokenizer



def get_qdrant_instance():
    global qdrant_client
    if qdrant_client is None:
        qdrant_client = QdrantClient(config.QDRANT_HOST)
        if not qdrant_client.collection_exists("vit_embeddings"):
            qdrant_client.create_collection("vit_embeddings", VectorParams(size=1024, distance=Distance.COSINE))
            qdrant_client.create_payload_index("vit_embeddings", "tags", "keyword")
            qdrant_client.create_payload_index("vit_embeddings", "page_no", "integer")
            qdrant_client.create_payload_index("vit_embeddings", "added_at", "integer")
        return qdrant_client
    return qdrant_client

@app.task()
def index_embedding_qdrant(image_ids):
    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")

        artworks = artwork_collection.find({"_id": {"$in": image_ids}, "vitEmbedding": {"$ne": None}})

        records = []
        for artwork in artworks:
            if artwork["vitEmbedding"] is not None:
                hash = hashlib.sha256()
                hash.update(artwork["_id"].encode("utf-8"))
                internal_id = uuid.UUID(hash.hexdigest()[:32]).hex

                records.append({
                    "internal_id": internal_id,
                    "vector": artwork["vitEmbedding"],
                    "payload": {
                        "image_id": artwork["_id"],
                        "tags": artwork["tags"],
                        "title": artwork["title"],
                        "page_no": artwork["page_no"],
                    }
                })

        if len(records) == 0:
            raise ValueError("No records to upload")

        qdrant_client = get_qdrant_instance()
        qdrant_client.upload_points("vit_embeddings", points=[
            PointStruct(id=doc["internal_id"], vector=doc["vector"], payload=doc["payload"])
            for idx, doc in enumerate(records)
        ])


@app.task()
def generate_embeddings(image_id):
    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")

        artwork = artwork_collection.find_one({"_id": image_id})

        if not artwork:
            raise ValueError("Artwork not found")

        model, preprocess, tokenizer = get_model_instances()

        # Download image
        with tempfile.TemporaryFile() as temp:
            res = requests.get(f"{s3.base_url}{artwork['s3_object_name']}")
            temp.write(res.content)
            print("Downloading {0} to {1}".format(res.url, temp.name))
            temp.seek(0)

            image = preprocess(Image.open(temp)).unsqueeze(0).to(config.VIT_DEVICE)
            text = tokenizer(", ".join(artwork["tags"])).to(config.VIT_DEVICE)

            print("Image and text prepared")

            # generate embeddings
            with torch.no_grad(), torch.cuda.amp.autocast():
                print("Running inference")
                image_features = model.encode_image(image)
                text_features = model.encode_text(text)

                image_features /= image_features.norm(dim=-1, keepdim=True)
                text_features /= text_features.norm(dim=-1, keepdim=True)


            print("Image features shape: {0}".format(image_features.shape))

            artwork_collection.update_one({"_id": image_id}, {"$set": {"vitEmbedding": image_features.cpu().numpy().tolist()[0]}})

            index_embedding_qdrant.delay([image_id])


@app.task()
def index_missing_images():
    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")

        artworks = artwork_collection.find({"vitEmbedding": None})

        for artwork in artworks:
            generate_embeddings.delay(artwork["_id"])


@app.task()
def get_similar(image_id, limit=25):
    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")

        artwork = artwork_collection.find_one({"_id": image_id})

        if not artwork:
            return

        qdrant_client = get_qdrant_instance()
        artwork = qdrant_client.search("vit_embeddings", query_vector=artwork["vitEmbedding"], limit=limit+5, with_payload=True)
        # Drop searched-for ID from results
        artwork = [res for res in artwork if res.payload["image_id"] != image_id]
        # Drop results that have the same pixiv_source_id as the searched-for ID (would probably all match, observed >0.95 filling all results).
        # Pixiv ID of 0 = not set
        artwork = [res for res in artwork if artwork_collection.find_one({"_id": res.payload["image_id"]})["pixiv_source_id"] != artwork_collection.find_one({"_id": image_id})["pixiv_source_id"]]
        # Limit to "limit" results
        artwork = artwork[:limit]

        hydrated_artwork = []
        for res in artwork:
            dbgArtwork = artwork_collection.find_one({"_id": res.payload["image_id"]})
            print("{0} - {1} -> {2}: {3}".format(res.score, res.payload["image_id"], res.payload["tags"], s3.base_url + dbgArtwork["s3_object_name"]).encode("utf-8"))
            hydrated_artwork.append(dbgArtwork | {"score": res.score})

        return hydrated_artwork


@app.task()
def neural_search(tags, page=1, limit=25):
    model, preprocess, tokenizer = get_model_instances()

    text = tokenizer(tags).to(config.VIT_DEVICE)

    # generate embeddings
    with torch.no_grad(), torch.cuda.amp.autocast():
        print("Running inference")
        text_features = model.encode_text(text)

        text_features /= text_features.norm(dim=-1, keepdim=True)

        qdrant_client = get_qdrant_instance()

        results = qdrant_client.search("vit_embeddings", query_vector=text_features.cpu().numpy().tolist()[0], limit=limit*page, with_payload=True)

        # Only return the requested page
        results = results[(page-1)*limit:page*limit]

        hydrated_artwork = []
        with MongoClient(CONNECTION_STRING) as connection:
            db = connection.get_database("supaarchive")
            artwork_collection = db.get_collection("artwork")
            for res in results:
                artwork = artwork_collection.find_one({"_id": res.payload["image_id"]})
                hydrated_artwork.append(artwork | {"score": res.score})
        return hydrated_artwork


@app.task()
def tag_search(tags, page=1, limit=25, group_sets=True):
    print("page: {0}, limit: {1}".format(page, limit))
    qdrant_client = get_qdrant_instance()

    offset_id = None

    img_filter = Filter(
        must=[
            FieldCondition(key="tags", match=MatchValue(value=tag))
            for _, tag in enumerate(tags)
        ],
        should=[
            FieldCondition(key="page_no", match=MatchValue(value=0)),
            IsNullCondition(is_null=PayloadField(key="page_no"))
        ] if group_sets else []
    )

    print("Filter: {0}".format(img_filter))

    while page > 1:
        _, next_page = qdrant_client.scroll("vit_embeddings", scroll_filter=img_filter, limit=limit, offset=offset_id)
        print("Next page: {0} ({1} -> {2})".format(next_page, page, page-1))
        if next_page is None:
            page = 1
        else:
            offset_id = next_page
        page -= 1

    results = qdrant_client.scroll("vit_embeddings", scroll_filter=img_filter, limit=limit, offset=offset_id, with_payload=True)[0]

    hydrated_artwork = []
    with MongoClient(CONNECTION_STRING) as connection:
        db = connection.get_database("supaarchive")
        artwork_collection = db.get_collection("artwork")
        for res in results:
            hydrated_artwork.append(artwork_collection.find_one({"_id": res.payload["image_id"]}))

    return hydrated_artwork


