import time
from typing import Annotated, Optional

import motor.motor_asyncio
from pydantic import BeforeValidator
from pydantic import ConfigDict, BaseModel, Field, EmailStr

import config

CONNECTION_STRING = config.MONGODB_CONNECTION_STRING

client = motor.motor_asyncio.AsyncIOMotorClient(CONNECTION_STRING)
db = client.get_database("supaarchive")

artwork_collection = db.get_collection("artwork")
translation_collection = db.get_collection("translations")

# Represents an ObjectId field in the database.
# It will be represented as a `str` on the model so that it can be serialized to JSON.
PyObjectId = Annotated[str, BeforeValidator(str)]


def get_unix_timestamp():
    return int(time.time())


class Artwork(BaseModel):
    id: PyObjectId = Field(alias="_id")
    s3_object_name: str
    tags: list

    added_at: int = Field(default_factory=get_unix_timestamp)

    page_no: Optional[int] = None
    author_name: Optional[str] = None

    title: Optional[str] = None
    description: Optional[str] = None
    pixiv_source_id: Optional[int] = None
    pixiv_author_id: Optional[int] = None
    gelbooru_id: Optional[int] = None

    vitEmbedding: Optional[str] = None
