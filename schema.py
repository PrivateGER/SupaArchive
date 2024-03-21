from pydantic import BaseModel


class PixivIndexPayload(BaseModel):
    illustration_id: int
    tags: list
    author_id: int
    author_name: str
    title: str
    description: str
    pages: list

class PixivDownloadBatch(BaseModel):
    url: str
    tags: list
    title: str
    description: str
    illust_id: int
    page_no: int
    author_id: int
    author_name: str
