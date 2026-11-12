"""A boto3-style S3 client on Google Cloud Storage, authenticated with a service account JSON key file.

Google data lake profiles (CANEDGE_CLOUD=google + CANEDGE_GOOGLE_KEY_FILE) use this instead of S3 interoperability
(HMAC) keys, so one key file covers the buckets and BigQuery. env.aws_client("s3") returns it; it answers the calls
the canedge-* skills make, with boto3's request arguments and response fields:

  get_paginator("list_objects_v2").paginate(Bucket, Prefix, Delimiter)   Contents (Key/Size/LastModified/ETag),
  list_objects_v2(Bucket, Prefix, Delimiter, MaxKeys, ContinuationToken) CommonPrefixes, KeyCount
  head_object / get_object(Bucket, Key)    ContentLength, LastModified, ETag, ContentType, StorageClass, Metadata
                                           (custom metadata, e.g. the CANedge 'timestamp'); get_object adds Body
  download_file(Bucket, Key, Filename), upload_file(Filename, Bucket, Key, ExtraArgs={"Metadata": ...}),
  put_object(Bucket, Key, Body, Metadata), delete_object(Bucket, Key), head_bucket(Bucket)

Errors are raised as botocore ClientError (404/403 for head_*, NoSuchKey/NoSuchBucket/AccessDenied otherwise), so
the scripts' S3 error handling works unchanged.
"""
from __future__ import annotations

import io

from botocore.exceptions import ClientError


def _client_error(op: str, e: Exception, head: bool) -> ClientError:
    from google.api_core import exceptions as gx
    if isinstance(e, gx.NotFound):
        code = "404" if head else ("NoSuchBucket" if "bucket" in str(e).lower() else "NoSuchKey")
        status = 404
    elif isinstance(e, gx.Forbidden):
        code, status = ("403" if head else "AccessDenied"), 403
    else:
        code, status = e.__class__.__name__, 400
    msg = getattr(e, "message", None) or str(e).splitlines()[0]
    return ClientError({"Error": {"Code": code, "Message": msg}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


class _Paginator:
    def __init__(self, client: "GcsS3"):
        self.client = client

    def paginate(self, Bucket: str, Prefix: str = "", Delimiter: str | None = None, **_):
        token = None
        while True:
            page = self.client.list_objects_v2(Bucket=Bucket, Prefix=Prefix, Delimiter=Delimiter,
                                               ContinuationToken=token)
            yield page
            token = page.get("NextContinuationToken")
            if not token:
                return


class GcsS3:
    """The S3 calls of the canedge-* skills on Google Cloud Storage."""

    def __init__(self, key_file: str, project: str | None = None):
        from google.cloud import storage
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(key_file)
        self.gcs = storage.Client(credentials=creds, project=project or creds.project_id)

    def _run(self, op: str, fn, head: bool = False):
        from google.api_core import exceptions as gx
        from google.auth import exceptions as ga
        try:
            return fn()
        except gx.GoogleAPICallError as e:
            raise _client_error(op, e, head) from None
        except ga.GoogleAuthError as e:   # invalid / revoked / deleted key
            raise ClientError({"Error": {"Code": "403" if head else "InvalidAccessKeyId",
                                         "Message": f"key rejected ({e.__class__.__name__})"},
                               "ResponseMetadata": {"HTTPStatusCode": 403}}, op) from None

    # ---------------------------------------------------------------- listing
    def get_paginator(self, name: str) -> _Paginator:
        if name != "list_objects_v2":
            raise NotImplementedError(f"gcs_s3: paginator {name}")
        return _Paginator(self)

    def list_objects_v2(self, Bucket: str, Prefix: str = "", Delimiter: str | None = None, MaxKeys: int = 1000,
                        ContinuationToken: str | None = None, **_) -> dict:
        def run():
            it = self.gcs.list_blobs(Bucket, prefix=Prefix or None, delimiter=Delimiter or None,
                                     page_token=ContinuationToken, page_size=min(MaxKeys, 1000))
            page = next(it.pages, None)
            blobs = list(page) if page is not None else []
            prefixes = sorted(getattr(page, "prefixes", ()) or ())
            return blobs, prefixes, it.next_page_token
        blobs, prefixes, token = self._run("ListObjectsV2", run)
        out = {"Contents": [{"Key": b.name, "Size": b.size, "LastModified": b.updated, "ETag": f'"{b.etag}"',
                             "StorageClass": b.storage_class} for b in blobs],
               "CommonPrefixes": [{"Prefix": p} for p in prefixes], "KeyCount": len(blobs) + len(prefixes),
               "IsTruncated": bool(token)}
        if token:
            out["NextContinuationToken"] = token
        return out

    def head_bucket(self, Bucket: str, **_) -> dict:
        self._run("HeadBucket", lambda: next(self.gcs.list_blobs(Bucket, max_results=1).pages, None), head=True)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    # ---------------------------------------------------------------- objects
    def _blob(self, Bucket: str, Key: str, op: str, head: bool):
        from google.api_core import exceptions as gx
        blob = self._run(op, lambda: self.gcs.bucket(Bucket).get_blob(Key), head)
        if blob is None:
            raise _client_error(op, gx.NotFound(f"No such object: {Bucket}/{Key}"), head)
        return blob

    @staticmethod
    def _fields(blob) -> dict:
        return {"ContentLength": blob.size, "LastModified": blob.updated, "ETag": f'"{blob.etag}"',
                "ContentType": blob.content_type, "StorageClass": blob.storage_class,
                "Metadata": dict(blob.metadata or {})}

    def head_object(self, Bucket: str, Key: str, **_) -> dict:
        return self._fields(self._blob(Bucket, Key, "HeadObject", head=True))

    def get_object(self, Bucket: str, Key: str, **_) -> dict:
        blob = self._blob(Bucket, Key, "GetObject", head=False)
        data = self._run("GetObject", blob.download_as_bytes)
        return {**self._fields(blob), "Body": io.BytesIO(data)}

    def download_file(self, Bucket: str, Key: str, Filename: str, **_) -> None:
        self._run("GetObject", lambda: self.gcs.bucket(Bucket).blob(Key).download_to_filename(Filename))

    def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: dict | None = None, **_) -> None:
        blob = self.gcs.bucket(Bucket).blob(Key)
        blob.metadata = (ExtraArgs or {}).get("Metadata") or None
        self._run("PutObject", lambda: blob.upload_from_filename(Filename))

    def put_object(self, Bucket: str, Key: str, Body=b"", Metadata: dict | None = None, **_) -> dict:
        blob = self.gcs.bucket(Bucket).blob(Key)
        blob.metadata = Metadata or None
        data = Body.read() if hasattr(Body, "read") else Body
        self._run("PutObject", lambda: blob.upload_from_string(data))
        return {"ETag": f'"{blob.etag}"'}

    def delete_object(self, Bucket: str, Key: str, **_) -> dict:
        self._run("DeleteObject", lambda: self.gcs.bucket(Bucket).blob(Key).delete())
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}
