import boto3
import botocore
from botocore.config import Config

import config

s3 = boto3.resource('s3',
    endpoint_url = config.S3_ENDPOINT,
    aws_access_key_id = config.S3_KEY_ID,
    aws_secret_access_key = config.S3_SECRET_KEY,
)

bucket = config.S3_BUCKET
base_url = config.S3_BASE_URL

def upload_file(file, object_name, extension):
    name = object_name + "." + extension
    s3.meta.client.upload_fileobj(file, bucket, name, ExtraArgs={'ACL':'public-read'})

    return name
