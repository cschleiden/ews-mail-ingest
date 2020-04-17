import config
import os
import logging
import json
import base64
import secrets
import datetime
import requests as py_requests
import tempfile

from urllib3 import exceptions as lib_exceptions
from exchangelib import Credentials, Account, Configuration, Folder, \
    FileAttachment, errors, Version, Build, FaultTolerance
from google.auth.transport.requests import AuthorizedSession
from google.resumable_media import requests, common
from google.cloud import kms_v1, storage, pubsub_v1
from PyPDF2 import PdfFileReader, PdfFileWriter

# Suppress warnings from exchangelib
logging.getLogger("exchangelib").setLevel(logging.ERROR)


class EWSMailMessage:
    def __init__(self, exchange_client, storage_client, bucket, bucket_name, publisher, topic_name, message, request):
        self.exchange_client = exchange_client
        self.storage_client = storage_client
        self.bucket = bucket
        self.bucket_name = bucket_name
        self.publisher = publisher
        self.topic_name = topic_name
        self.message = message
        self.request = request
        self.path = self.set_message_path()

    def process(self):
        logging.info('Started processing of e-mail')

        # Save original message to bucket
        try:
            message_text_blob = self.bucket.blob('%s/original_email.html' % self.path)
            message_text_blob.upload_from_string(self.message.unique_body)
        finally:
            logging.info("Finished upload of original e-mail content")

        message_attachments = self.process_message_attachments()
        self.process_message_status()
        self.process_message_meta(attachments=message_attachments)

        logging.info('Finished processing of e-mail')

    def process_message_attachments(self):
        message_attachments = []
        for attachment in self.message.attachments:
            if isinstance(attachment, FileAttachment) and attachment.content_type in ['text/xml', 'application/pdf']:
                clean_attachment_name = attachment.name.replace(' ', '_'). \
                    replace('.', '_', attachment.name.count('.') - 1).replace('-', '_')

                try:
                    file_path = '%s/%s' % (self.path, clean_attachment_name)

                    # Clean PDF from malicious content
                    if attachment.content_type == 'application/pdf':
                        writer = PdfFileWriter()  # Create a PdfFileWriter to store the new PDF
                        with tempfile.NamedTemporaryFile(delete=True) as temp_file:
                            temp_file.write(attachment.content)
                            reader = PdfFileReader(open(temp_file.name, 'rb'))
                            [writer.addPage(reader.getPage(i)) for i in range(0, reader.getNumPages())]
                            writer.removeLinks()
                            with tempfile.NamedTemporaryFile(mode='w+b', delete=True) as temp_flat_file:
                                writer.write(temp_flat_file)
                                self.write_stream_to_blob(self.bucket_name, file_path, open(temp_flat_file.name, 'rb'))
                                temp_flat_file.close()
                            temp_file.close()
                    else:
                        self.write_stream_to_blob(self.bucket_name, file_path, attachment.fp)

                    message_attachments.append({
                        'name': clean_attachment_name,
                        'path': f'gs://{config.GCP_BUCKET_NAME}/{file_path}',
                        'content_type': attachment.content_type,
                    })
                finally:
                    logging.info("Finished upload of attachment '{}'".format(clean_attachment_name))

        return message_attachments

    def write_stream_to_blob(self, bucket_name, path, content):
        with GCSObjectStreamUpload(
                client=self.storage_client, bucket_name=bucket_name, blob_name=path) as f, content as fp:
            buffer = fp.read(1024)
            while buffer:
                f.write(buffer)
                buffer = fp.read(1024)

    def process_message_status(self):
        try:
            self.message.is_read = True
            self.message.save(update_fields=['is_read'])
            self.message.move(self.exchange_client.inbox / config.EXCHANGE_FOLDER_NAME)
        finally:
            logging.info('Finished moving of e-mail')

    def process_message_meta(self, attachments):
        try:
            message_meta = {
                'gcp_project': os.environ.get('GCP_PROJECT', ''),
                'execution_id': self.request.headers.get('Function-Execution-Id', ''),
                'execution_type': 'cloud_function',
                'execution_name': os.environ.get('FUNCTION_NAME', ''),
                'execution_trigger_type': os.environ.get('FUNCTION_TRIGGER_TYPE', ''),
                'timestamp': datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            }

            message_data = {
                'message_id': self.message.id,
                'sender': self.message.sender.email_address,
                'receiver': self.message.received_by.email_address,
                'subject': self.message.subject,
                'datetime_sent': self.message.datetime_sent.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                'datetime_received': self.message.datetime_received.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                'original_email': self.message.unique_body,
                'attachments': attachments
            }
            meta = {'gobits': [message_meta], 'mail': message_data}

            # Save meta file to bucket
            blob = self.bucket.blob('{}/metadata.json'.format(self.path))
            blob.upload_from_string(json.dumps(meta), content_type='application/json')

            # Publish message to topic
            self.publisher.publish(self.topic_name, bytes(json.dumps(meta).encode('utf-8')))
        finally:
            logging.info('Finished posting of e-mail meta to Pub/Sub')

    def set_message_path(self):
        now = self.message.datetime_sent
        message_id = '%04d%02d%02dT%02d%02d%02dZ' % (now.year, now.month, now.day, now.hour, now.minute, now.second)
        path = '%s/%04d/%02d/%02d/%s' % (config.EXCHANGE_USERNAME, now.year, now.month, now.day, message_id)

        if self.check_gcs_blob_exists(f"{path}/original_email.html"):
            path = '{}_{}'.format(path, secrets.randbits(64))

        return path

    # Check if blob already exists
    def check_gcs_blob_exists(self, name):
        return storage.Blob(bucket=self.bucket, name=name).exists(self.storage_client)


class EWSMailIngest:
    def __init__(self, request):
        # Decode KMS encrypted password
        exchange_password_encrypted = base64.b64decode(os.environ['EXCHANGE_PASSWORD_ENCRYPTED'])
        kms_client = kms_v1.KeyManagementServiceClient()
        crypto_key_name = kms_client.crypto_key_path_path(os.environ['PROJECT_ID'], 'europe', 'ews-api',
                                                          'ews-api-credentials')
        decrypt_response = kms_client.decrypt(crypto_key_name, exchange_password_encrypted)
        exchange_password = decrypt_response.plaintext.decode("utf-8").replace('\n', '')

        # Initialize connection to Exchange Web Services
        acc_credentials = Credentials(username=config.EXCHANGE_USERNAME, password=exchange_password)
        version = Version(build=Build(config.EXCHANGE_VERSION['major'], config.EXCHANGE_VERSION['minor']))
        acc_config = Configuration(service_endpoint=config.EXCHANGE_URL, credentials=acc_credentials,
                                   auth_type='basic', version=version, retry_policy=FaultTolerance(max_wait=300))
        self.exchange_client = Account(primary_smtp_address=config.EXCHANGE_USERNAME, config=acc_config,
                                       autodiscover=False, access_type='delegate')

        self.storage_client = storage.Client()
        self.bucket = self.storage_client.get_bucket(config.GCP_BUCKET_NAME)
        self.bucket_name = config.GCP_BUCKET_NAME
        self.request = request
        self.publisher = pubsub_v1.PublisherClient()
        self.topic_name = 'projects/{project_id}/topics/{topic}'.format(project_id=config.TOPIC_PROJECT_ID,
                                                                        topic=config.TOPIC_NAME)

    def initialize_exchange_account(self):
        try:
            processed_folder = Folder(parent=self.exchange_client.inbox, name=config.EXCHANGE_FOLDER_NAME)
            processed_folder.save()
        except errors.ErrorFolderExists:
            pass

    def process(self):
        try:
            self.initialize_exchange_account()

            if self.exchange_client and self.exchange_client.inbox:
                if self.exchange_client.inbox.unread_count > 0:
                    logging.info('Found {} unread e-mails'.format(self.exchange_client.inbox.unread_count))

                    inbox_query = self.exchange_client.inbox.filter(is_read=False).order_by('-datetime_received')
                    inbox_query.page_size = 2

                    for message in inbox_query.iterator():
                        EWSMailMessage(exchange_client=self.exchange_client,
                                       storage_client=self.storage_client,
                                       bucket=self.bucket,
                                       bucket_name=self.bucket_name,
                                       publisher=self.publisher,
                                       topic_name=self.topic_name,
                                       message=message,
                                       request=self.request).process()
                else:
                    logging.info('No unread e-mails in mailbox')
            else:
                logging.warning('Can\'t find the inbox')
        except (KeyError, ConnectionResetError, py_requests.exceptions.ConnectionError,
                lib_exceptions.ProtocolError) as e:
            logging.warning(str(e))
        except Exception as e:
            logging.exception(e)


class GCSObjectStreamUpload(object):
    def __init__(
            self,
            client: storage.Client,
            bucket_name: str,
            blob_name: str,
            chunk_size: int = 256 * 1024
    ):
        self._client = client
        self._bucket = self._client.bucket(bucket_name)
        self._blob = self._bucket.blob(blob_name)

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
            content_type='application/octet-stream',
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
        # I'm not good with efficient no-copy buffering so if this is
        # wrong or there's a better way to do this let me know! :-)
        to_read = min(chunk_size, self._buffer_size)
        memview = memoryview(self._buffer)
        self._buffer = memview[to_read:].tobytes()
        self._read += to_read
        self._buffer_size -= to_read
        return memview[:to_read].tobytes()

    def tell(self) -> int:
        return self._read


def ews_to_bucket(request):
    if request.method == 'POST':
        EWSMailIngest(request=request).process()


if __name__ == '__main__':
    mock_request = py_requests.session()
    mock_request.method = "POST"
    logging.getLogger().setLevel(logging.INFO)

    ews_to_bucket(mock_request)
