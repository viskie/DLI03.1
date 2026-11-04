"""Encrypted private-file storage on Vercel Blob with conditional writes."""
import json
import hashlib
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
import requests
from cryptography.fernet import Fernet

class StoreConflict(RuntimeError):
    pass

class BlobHTTPClient:
    """Uses the same HTTP endpoints and headers as Vercel's Blob SDK."""
    def __init__(self, token=None):
        self.token=token or os.environ['BLOB_READ_WRITE_TOKEN']
        match=re.fullmatch(r'vercel_blob_rw_([A-Za-z0-9]+)_.+',self.token)
        if not match:
            raise RuntimeError('Invalid Blob storage credentials.')
        self.store_id=match.group(1).lower()
        self.headers={'Authorization':'Bearer '+self.token,'x-api-version':'12'}

    def _metadata(self,path):
        response=requests.get('https://vercel.com/api/blob',params={'url':path},headers=self.headers,timeout=20)
        if response.status_code==404:
            return None
        response.raise_for_status()
        return response.json()

    def _download(self,path):
        url=f'https://{self.store_id}.private.blob.vercel-storage.com/'+quote(path,safe='/')
        response=requests.get(url,headers={**self.headers,'Cache-Control':'no-cache','Accept-Encoding':'identity'},timeout=20)
        return response

    def get(self,path,**kwargs):
        metadata=self._metadata(path)
        if metadata is None:
            return None
        etag=metadata['etag']
        version=etag.strip('"')
        # Delivery caches can lag overwrites. Content-addressed snapshots never change.
        response=self._download(path+'.versions/'+version+'.enc')
        if response.status_code==404:
            # Migration fallback for a file created before versioned snapshots existed.
            response=self._download(path)
        response.raise_for_status()
        actual=hashlib.md5(response.content).hexdigest()
        if actual!=version:
            raise StoreConflict('Account storage is refreshing. Please retry.')
        return SimpleNamespace(status_code=200,stream=iter([response.content]),blob=SimpleNamespace(etag=etag))

    def _put(self,path,body,allow_overwrite=False,if_match=None):
        headers={**self.headers,'x-vercel-blob-access':'private','x-add-random-suffix':'0',
                 'x-allow-overwrite':'1' if allow_overwrite else '0',
                 'x-content-type':'application/octet-stream','x-cache-control-max-age':'0'}
        if if_match:
            headers['x-if-match']=if_match
        response=requests.put('https://vercel.com/api/blob',params={'pathname':path},headers=headers,data=body,timeout=25)
        if response.status_code==412:
            raise StoreConflict('Another request updated the account file. Please retry.')
        response.raise_for_status()
        return response.json()

    def put(self,path,body,**kwargs):
        # Upload the exact immutable revision before atomically changing the current file.
        version=hashlib.md5(body).hexdigest()
        self._put(path+'.versions/'+version+'.enc',body,allow_overwrite=True)
        return self._put(path,body,allow_overwrite=kwargs.get('allow_overwrite',False),if_match=kwargs.get('if_match'))

class BlobStore:
    def __init__(self,client=None):
        self.client=client or BlobHTTPClient()
        self.path=Path('/tmp/oracle691')
        self.cipher=Fernet(os.environ['ORACLE_DATA_KEY'])
        self.pathname=os.getenv('ORACLE_BLOB_PATH','oracle691/accounts.enc')

    @contextmanager
    def transaction(self):
        result=self.client.get(self.pathname,access='private')
        if result is None:
            raise RuntimeError('The encrypted account file has not been initialized.')
        if result.status_code!=200 or result.stream is None:
            raise RuntimeError('The account file is unavailable.')
        data=json.loads(self.cipher.decrypt(b''.join(result.stream)))
        before=json.dumps(data,sort_keys=True,separators=(',',':'))
        yield data
        after=json.dumps(data,sort_keys=True,separators=(',',':'))
        if after!=before:
            self.client.put(self.pathname,self.cipher.encrypt(after.encode()),access='private',
                            add_random_suffix=False,allow_overwrite=True,if_match=result.blob.etag)
