"""File and folder creation, file transfer operations."""

import http.client as http
import os
import json
import io
import mimetypes
from collections import OrderedDict
import logging
from urllib.parse import quote_plus
from requests import Response

try:
    from requests_toolbelt import MultipartEncoder
except ImportError:
    from acdcli.bundled.encoder import MultipartEncoder

from .common import *

FS_RW_CHUNK_SZ = 1024 * 128

PARTIAL_SUFFIX = '.__incomplete'
CHUNK_SIZE = 500 * 1024 ** 2  # basically arbitrary
CHUNK_MAX_RETRY = 5
CONSECUTIVE_DL_LIMIT = CHUNK_SIZE

logger = logging.getLogger(__name__)


class _TeeBufferedReader(object):
    """Creates proxy buffered reader object that allows callbacks on read operations."""

    def __init__(self, file: io.BufferedReader, callbacks: list = None):
        self._file = file
        self._callbacks = callbacks

    def __getattr__(self, item):
        try:
            return object.__getattr__(item)
        except AttributeError:
            return getattr(self._file, item)

    def read(self, ln=-1):
        ln = ln if ln in (0, -1) else FS_RW_CHUNK_SZ
        chunk = self._file.read(ln)
        for callback in self._callbacks or []:
            callback(chunk)
        return chunk


def _tee_open(path: str, **kwargs) -> _TeeBufferedReader:
    f = open(path, 'rb')
    return _TeeBufferedReader(f, **kwargs)


def _get_mimetype(file_name: str = '') -> str:
    mt = mimetypes.guess_type(file_name)[0]
    return mt if mt else 'application/octet-stream'


def _multipart_stream(metadata: dict, stream, boundary: str, read_callbacks=None):
    """Generator for chunked multipart/form-data file upload from stream input.
    :param metadata: file info, leave empty for overwrite
    :param stream: readable object
    """

    if metadata:
        yield str.encode('--%s\r\nContent-Disposition: form-data; '
                         'name="metadata"\r\n\r\n' % boundary +
                         '%s\r\n' % json.dumps(metadata))
    yield str.encode('--%s\r\n' % boundary) + \
        b'Content-Disposition: form-data; name="content"; filename="foo"\r\n' + \
        b'Content-Type: application/octet-stream\r\n\r\n'
    while True:
        f = stream.read(FS_RW_CHUNK_SZ)
        if f:
            for cb in read_callbacks or []:
                cb(f)
            yield f
        else:
            break
    yield str.encode('\r\n--%s--\r\n' % boundary +
                     'multipart/form-data; boundary=%s' % boundary)


class ContentMixin(object):
    def create_folder(self, name: str, parent=None) -> dict:
        body = {'kind': 'FOLDER', 'name': name}
        if parent:
            body['parents'] = [parent]
        body_str = json.dumps(body)

        acc_codes = [http.CREATED]

        r = self.BOReq.post(self.metadata_url + 'nodes', acc_codes=acc_codes, data=body_str)

        if r.status_code not in acc_codes:
            raise RequestError(r.status_code, r.text)

        return r.json()

    def create_file(self, file_name: str, parent: str = None) -> dict:
        params = {'suppress': 'deduplication'}

        basename = os.path.basename(file_name)
        metadata = {'kind': 'FILE', 'name': basename}
        if parent:
            metadata['parents'] = [parent]
        mime_type = _get_mimetype(basename)
        f = io.BytesIO()

        # basename is ignored
        m = MultipartEncoder(fields=OrderedDict([('metadata', json.dumps(metadata)),
                                                 ('content', (quote_plus(basename), f, mime_type))])
                             )

        ok_codes = [http.CREATED]
        r = self.BOReq.post(self.content_url + 'nodes', params=params, data=m,
                            acc_codes=ok_codes, headers={'Content-Type': m.content_type})

        if r.status_code not in ok_codes:
            raise RequestError(r.status_code, r.text)
        return r.json()

    def clear_file(self, node_id: str) -> dict:
        m = MultipartEncoder(fields={('content', (' ', io.BytesIO(), _get_mimetype()))})

        r = self.BOReq.put(self.content_url + 'nodes/' + node_id + '/content', params={},
                           data=m, stream=True, headers={'Content-Type': m.content_type})

        if r.status_code not in OK_CODES:
            raise RequestError(r.status_code, r.text)

        return r.json()

    def upload_file(self, file_name: str, parent: str = None,
                    read_callbacks=None, deduplication=False) -> dict:
        params = {'suppress': 'deduplication'}
        if deduplication and os.path.getsize(file_name) > 0:
            params = {}

        basename = os.path.basename(file_name)
        metadata = {'kind': 'FILE', 'name': basename}
        if parent:
            metadata['parents'] = [parent]
        mime_type = _get_mimetype(basename)
        f = _tee_open(file_name, callbacks=read_callbacks)

        # basename is ignored
        m = MultipartEncoder(fields=OrderedDict([('metadata', json.dumps(metadata)),
                                                 (
                                                     'content',
                                                     (quote_plus(basename), f, mime_type))]))

        ok_codes = [http.CREATED]
        r = self.BOReq.post(self.content_url + 'nodes', params=params, data=m,
                            acc_codes=ok_codes, stream=True,
                            headers={'Content-Type': m.content_type})

        if r.status_code not in ok_codes:
            raise RequestError(r.status_code, r.text)
        return r.json()

    def upload_stream(self, stream, file_name: str, parent: str = None,
                      read_callbacks=None, deduplication=False) -> dict:
        params = {} if deduplication else {'suppress': 'deduplication'}

        metadata = {'kind': 'FILE', 'name': file_name}
        if parent:
            metadata['parents'] = [parent]

        import uuid
        boundary = uuid.uuid4().hex

        ok_codes = [http.CREATED]
        r = self.BOReq.post(self.content_url + 'nodes', params=params,
                            data=_multipart_stream(metadata, stream, boundary, read_callbacks),
                            acc_codes=ok_codes,
                            headers={'Content-Type': 'multipart/form-data; boundary=%s'
                                                     % boundary})

        if r.status_code not in ok_codes:
            raise RequestError(r.status_code, r.text)
        return r.json()

    def overwrite_file(self, node_id: str, file_name: str,
                       read_callbacks=None, deduplication=False) -> dict:
        params = {} if deduplication else {'suppress': 'deduplication'}

        basename = os.path.basename(file_name)
        mime_type = _get_mimetype(basename)
        f = _tee_open(file_name, callbacks=read_callbacks)

        # basename is ignored
        m = MultipartEncoder(fields={('content', (quote_plus(basename), f, mime_type))})

        r = self.BOReq.put(self.content_url + 'nodes/' + node_id + '/content', params=params,
                           data=m, stream=True, headers={'Content-Type': m.content_type})

        if r.status_code not in OK_CODES:
            raise RequestError(r.status_code, r.text)

        return r.json()

    def overwrite_stream(self, stream, node_id, read_callbacks=None) -> dict:
        metadata = {}
        import uuid
        boundary = uuid.uuid4().hex

        r = self.BOReq.put(self.content_url + 'nodes/' + node_id + '/content',
                           data=_multipart_stream(metadata, stream, boundary, read_callbacks),
                           headers={'Content-Type': 'multipart/form-data; boundary=%s'
                                                    % boundary})

        if r.status_code not in OK_CODES:
            raise RequestError(r.status_code, r.text)
        return r.json()

    def download_file(self, node_id: str, basename: str, dirname: str = None, **kwargs):
        """ Deals with download preparation, download with :func:`chunked_download` and finish.
        Calls callbacks while fast forwarding through incomplete file (if existent).
        Will not check for existing file prior to download and overwrite existing file on finish.
        :param dirname: a valid local directory name, or CWD if None
        :param basename: a valid file name
        kwargs:
        length: the total length of the file
        write_callbacks (list[function]): passed on to :func:`chunked_download`
        resume (bool=True): whether to resume if partial file exists
        """

        dl_path = basename
        if dirname:
            dl_path = os.path.join(dirname, basename)
        part_path = dl_path + PARTIAL_SUFFIX
        offset = 0

        length = kwargs.get('length', 0)
        resume = kwargs.get('resume', True)
        if resume and os.path.isfile(part_path):
            with open(part_path, 'ab') as f:
                trunc_pos = os.path.getsize(part_path) - 1 - FS_RW_CHUNK_SZ
                f.truncate(trunc_pos if trunc_pos >= 0 else 0)

            write_callbacks = kwargs.get('write_callbacks')
            if write_callbacks:
                with open(part_path, 'rb') as f:
                    for chunk in iter(lambda: f.read(FS_RW_CHUNK_SZ), b''):
                        for rcb in write_callbacks:
                            rcb(chunk)

            f = open(part_path, 'ab')
        else:
            f = open(part_path, 'wb')
        offset = f.tell()

        self.chunked_download(node_id, f, offset=offset, **kwargs)
        pos = f.tell()
        f.close()
        if length > 0 and pos < length:
            raise RequestError(RequestError.CODE.INCOMPLETE_RESULT,
                               '[acd_cli] download incomplete.')

        if os.path.isfile(dl_path):
            logger.info('Deleting existing file "%s".' % dl_path)
            os.remove(dl_path)
        os.rename(part_path, dl_path)

    @catch_conn_exception
    def chunked_download(self, node_id: str, file: io.BufferedWriter, **kwargs):
        """Keyword args:
        offset (int): byte offset -- start byte for ranged request
        length (int): total file length[!], equal to end + 1
        write_callbacks (list[function])
        """
        ok_codes = [http.PARTIAL_CONTENT]

        write_callbacks = kwargs.get('write_callbacks', [])

        chunk_start = kwargs.get('offset', 0)
        length = kwargs.get('length', 100 * 1024 ** 4)

        retries = 0
        while chunk_start < length:
            chunk_end = chunk_start + CHUNK_SIZE - 1
            if chunk_end >= length:
                chunk_end = length - 1

            if retries >= CHUNK_MAX_RETRY:
                raise RequestError(RequestError.CODE.FAILED_SUBREQUEST,
                                   '[acd_cli] Downloading chunk failed multiple times.')
            r = self.BOReq.get(self.content_url + 'nodes/' + node_id + '/content', stream=True,
                               acc_codes=ok_codes,
                               headers={'Range': 'bytes=%d-%d' % (chunk_start, chunk_end)})

            logger.debug('Range %d-%d' % (chunk_start, chunk_end))
            # this should only happen at the end of unknown-length downloads
            if r.status_code == http.REQUESTED_RANGE_NOT_SATISFIABLE:
                logger.debug('Invalid byte range requested %d-%d' % (chunk_start, chunk_end))
                break
            if r.status_code not in ok_codes:
                r.close()
                retries += 1
                logging.debug('Chunk [%d-%d], retry %d.' % (chunk_start, chunk_end, retries))
                continue

            curr_ln = 0
            try:
                for chunk in r.iter_content(chunk_size=FS_RW_CHUNK_SZ):
                    if chunk:  # filter out keep-alive new chunks
                        file.write(chunk)
                        file.flush()
                        for wcb in write_callbacks:
                            wcb(chunk)
                        curr_ln += len(chunk)
            finally:
                r.close()

            chunk_start += CHUNK_SIZE
            retries = 0

        return

    def response_chunk(self, node_id: str, offset: int, length: int, **kwargs) -> Response:
        ok_codes = [http.PARTIAL_CONTENT]
        end = offset + length - 1
        logger.debug('chunk o %d l %d' % (offset, length))

        r = self.BOReq.get(self.content_url + 'nodes/' + node_id + '/content',
                           acc_codes=ok_codes, stream=True,
                           headers={'Range': 'bytes=%d-%d' % (offset, end)}, **kwargs)
        # if r.status_code == http.REQUESTED_RANGE_NOT_SATISFIABLE:
        #     return
        if r.status_code not in ok_codes:
            raise RequestError(r.status_code, r.text)

        return r

    def download_chunk(self, node_id: str, offset: int, length: int, **kwargs):
        """:param length: the length of the download chunk"""
        r = self.response_chunk(node_id, offset, length, **kwargs)
        if not r:
            return

        buffer = bytearray()
        try:
            for chunk in r.iter_content(chunk_size=FS_RW_CHUNK_SZ):
                if chunk:
                    buffer.extend(chunk)
        finally:
            r.close()
        return buffer

    def download_thumbnail(self, node_id: str, file_name: str, max_dim=128):
        """Download a movie's/picture's thumbnail into a file.
        Officially supports the image formats JPEG, BMP, PNG, TIFF, some RAW formats
        and the video formats MP4, QuickTime, AVI, MTS, MPEG, ASF, WMV, FLV, OGG.
        See http://www.amazon.com/gp/help/customer/display.html?nodeId=201634590
        Additionally supports MKV.
        :param max_dim: maximum width or height of the resized image/video thumbnail
        """

        r = self.BOReq.get(self.content_url + 'nodes/' + node_id + '/content',
                           params={'viewBox': max_dim}, stream=True)
        if r.status_code not in OK_CODES:
            raise RequestError(r.status_code, r.text)
        try:
            with open(file_name, 'wb') as f:
                f.write(r.raw.read())
        finally:
            r.close()
