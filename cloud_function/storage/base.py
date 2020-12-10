import logging

from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
from google.resumable_media import requests, common

from storage.cleaners import FileCleaner


class GCSObjectStreamUpload(object):
    def __init__(
            self,
            client: storage.Client,
            bucket_name: str,
            blob_name: str,
            content_type: str,
            chunk_size: int = 256 * 1024
    ):
        self._client = client
        self._bucket = self._client.bucket(bucket_name)
        self._blob = self._bucket.blob(blob_name)
        self._content_type = content_type

        self._buffer = b''
        self._buffer_size = 0
        self._chunk_size = chunk_size
        self._read = 0

        self._transport = AuthorizedSession(
            credentials=self._client._credentials
        )
        self._request = None  # type: requests.ResumableUpload

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.stop()

    def start(self):
        url = (
            f'https://www.googleapis.com/upload/storage/v1/b/'
            f'{self._bucket.name}/o?uploadType=resumable'
        )
        self._request = requests.ResumableUpload(
            upload_url=url, chunk_size=self._chunk_size
        )
        self._request.initiate(
            transport=self._transport,
            content_type=self._content_type,
            stream=self,
            stream_final=False,
            metadata={'name': self._blob.name},
        )

    def stop(self):
        self._request.transmit_next_chunk(self._transport)

    def write(self, data: bytes) -> int:
        data_len = len(data)
        self._buffer_size += data_len
        self._buffer += data
        del data
        while self._buffer_size >= self._chunk_size:
            try:
                self._request.transmit_next_chunk(self._transport)
            except common.InvalidResponse:
                self._request.recover(self._transport)
        return data_len

    def read(self, chunk_size: int) -> bytes:
        to_read = min(chunk_size, self._buffer_size)
        memview = memoryview(self._buffer)
        self._buffer = memview[to_read:].tobytes()
        self._read += to_read
        self._buffer_size -= to_read
        return memview[:to_read].tobytes()

    def tell(self) -> int:
        return self._read


class StorageService:
    bucket: str
    credentials = None

    def __init__(self, bucket: str):
        self.bucket = bucket
        self.storage_client = storage.Client()

    def _store_file(self, file, filename: str, content_type: str = None):
        file = FileCleaner(file, filename, content_type).clean()

        with GCSObjectStreamUpload(client=self.storage_client,
                                   bucket_name=self.bucket,
                                   blob_name=filename,
                                   content_type=content_type) as f,\
                file as fp:
            buffer = fp.read(1024)
            while buffer:
                f.write(buffer)
                buffer = fp.read(1024)

        logging.info(
            "File uploaded to bucket {} with filename {}.".format(
                self.bucket,
                filename
            )
        )

        return