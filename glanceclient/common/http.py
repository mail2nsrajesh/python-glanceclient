# Copyright 2012 OpenStack LLC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import httplib
import logging
import os
import socket
import StringIO
import struct
import urlparse

try:
    import json
except ImportError:
    import simplejson as json

# Python 2.5 compat fix
if not hasattr(urlparse, 'parse_qsl'):
    import cgi
    urlparse.parse_qsl = cgi.parse_qsl

import OpenSSL

from glanceclient import exc


LOG = logging.getLogger(__name__)
USER_AGENT = 'python-glanceclient'
CHUNKSIZE = 1024 * 64  # 64kB


class HTTPClient(object):

    def __init__(self, endpoint, **kwargs):
        self.endpoint = endpoint
        endpoint_parts = self.parse_endpoint(self.endpoint)
        self.endpoint_scheme = endpoint_parts.scheme
        self.endpoint_hostname = endpoint_parts.hostname
        self.endpoint_port = endpoint_parts.port
        self.endpoint_path = endpoint_parts.path

        self.connection_class = self.get_connection_class(self.endpoint_scheme)
        self.connection_kwargs = self.get_connection_kwargs(
                self.endpoint_scheme, **kwargs)

        self.auth_token = kwargs.get('token')

    @staticmethod
    def parse_endpoint(endpoint):
        return urlparse.urlparse(endpoint)

    @staticmethod
    def get_connection_class(scheme):
        if scheme == 'https':
            return VerifiedHTTPSConnection
        else:
            return httplib.HTTPConnection

    @staticmethod
    def get_connection_kwargs(scheme, **kwargs):
        _kwargs = {'timeout': float(kwargs.get('timeout', 600))}

        if scheme == 'https':
            _kwargs['ca_file'] = kwargs.get('ca_file', None)
            _kwargs['cert_file'] = kwargs.get('cert_file', None)
            _kwargs['key_file'] = kwargs.get('key_file', None)
            _kwargs['insecure'] = kwargs.get('insecure', False)
            _kwargs['ssl_compression'] = kwargs.get('ssl_compression', True)

        return _kwargs

    def get_connection(self):
        _class = self.connection_class
        try:
            return _class(self.endpoint_hostname, self.endpoint_port,
                          **self.connection_kwargs)
        except httplib.InvalidURL:
            raise exc.InvalidEndpoint()

    def log_curl_request(self, method, url, kwargs):
        curl = ['curl -i -X %s' % method]

        for (key, value) in kwargs['headers'].items():
            header = '-H \'%s: %s\'' % (key, value)
            curl.append(header)

        conn_params_fmt = [
            ('key_file', '--key %s'),
            ('cert_file', '--cert %s'),
            ('ca_file', '--cacert %s'),
        ]
        for (key, fmt) in conn_params_fmt:
            value = self.connection_kwargs.get(key)
            if value:
                curl.append(fmt % value)

        if self.connection_kwargs.get('insecure'):
            curl.append('-k')

        if 'body' in kwargs:
            curl.append('-d \'%s\'' % kwargs['body'])

        curl.append('%s%s' % (self.endpoint, url))
        LOG.debug(' '.join(curl))

    @staticmethod
    def log_http_response(resp, body=None):
        status = (resp.version / 10.0, resp.status, resp.reason)
        dump = ['\nHTTP/%.1f %s %s' % status]
        dump.extend(['%s: %s' % (k, v) for k, v in resp.getheaders()])
        dump.append('')
        if body:
            dump.extend([body, ''])
        LOG.debug('\n'.join(dump))

    def _http_request(self, url, method, **kwargs):
        """ Send an http request with the specified characteristics.

        Wrapper around httplib.HTTP(S)Connection.request to handle tasks such
        as setting headers and error handling.
        """
        # Copy the kwargs so we can reuse the original in case of redirects
        kwargs['headers'] = copy.deepcopy(kwargs.get('headers', {}))
        kwargs['headers'].setdefault('User-Agent', USER_AGENT)
        if self.auth_token:
            kwargs['headers'].setdefault('X-Auth-Token', self.auth_token)

        self.log_curl_request(method, url, kwargs)
        conn = self.get_connection()

        try:
            conn_url = os.path.normpath('%s/%s' % (self.endpoint_path, url))
            conn.request(method, conn_url, **kwargs)
            resp = conn.getresponse()
        except socket.gaierror as e:
            message = "Error finding address for %(url)s: %(e)s" % locals()
            raise exc.InvalidEndpoint(message=message)
        except (socket.error, socket.timeout) as e:
            endpoint = self.endpoint
            message = "Error communicating with %(endpoint)s %(e)s" % locals()
            raise exc.CommunicationError(message=message)

        body_iter = ResponseBodyIterator(resp)

        # Read body into string if it isn't obviously image data
        if resp.getheader('content-type', None) != 'application/octet-stream':
            body_str = ''.join([chunk for chunk in body_iter])
            self.log_http_response(resp, body_str)
            body_iter = StringIO.StringIO(body_str)
        else:
            self.log_http_response(resp)

        if 400 <= resp.status < 600:
            LOG.error("Request returned failure status.")
            raise exc.from_response(resp)
        elif resp.status in (301, 302, 305):
            # Redirected. Reissue the request to the new location.
            return self._http_request(resp['location'], method, **kwargs)
        elif resp.status == 300:
            raise exc.from_response(resp)

        return resp, body_iter

    def json_request(self, method, url, **kwargs):
        kwargs.setdefault('headers', {})
        kwargs['headers'].setdefault('Content-Type', 'application/json')

        if 'body' in kwargs:
            kwargs['body'] = json.dumps(kwargs['body'])

        resp, body_iter = self._http_request(url, method, **kwargs)

        if 'application/json' in resp.getheader('content-type', None):
            body = ''.join([chunk for chunk in body_iter])
            try:
                body = json.loads(body)
            except ValueError:
                LOG.error('Could not decode response body as JSON')
        else:
            body = None

        return resp, body

    def raw_request(self, method, url, **kwargs):
        kwargs.setdefault('headers', {})
        kwargs['headers'].setdefault('Content-Type',
                                     'application/octet-stream')
        return self._http_request(url, method, **kwargs)


class OpenSSLConnectionDelegator(object):
    """
    An OpenSSL.SSL.Connection delegator.

    Supplies an additional 'makefile' method which httplib requires
    and is not present in OpenSSL.SSL.Connection.

    Note: Since it is not possible to inherit from OpenSSL.SSL.Connection
    a delegator must be used.
    """
    def __init__(self, *args, **kwargs):
        self.connection = OpenSSL.SSL.Connection(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def makefile(self, *args, **kwargs):
        return socket._fileobject(self.connection, *args, **kwargs)


class VerifiedHTTPSConnection(httplib.HTTPSConnection):
    """
    Extended HTTPSConnection which uses the OpenSSL library
    for enhanced SSL support.
    """
    def __init__(self, host, port, key_file=None, cert_file=None,
                 ca_file=None, timeout=None, insecure=False,
                 ssl_compression=True):
        httplib.HTTPSConnection.__init__(self, host, port,
                                         key_file=key_file,
                                         cert_file=cert_file)
        self.key_file = key_file
        self.cert_file = cert_file
        self.timeout = timeout
        self.insecure = insecure
        self.ssl_compression = ssl_compression
        self.ca_file = ca_file
        self.setcontext()

    @staticmethod
    def verify_callback(connection, x509, errnum, errdepth, preverify_ok):
        # Pass through OpenSSL's default result
        return preverify_ok

    def setcontext(self):
        """
        Set up the OpenSSL context.
        """
        self.context = OpenSSL.SSL.Context(OpenSSL.SSL.SSLv23_METHOD)

        if self.ssl_compression is False:
            self.context.set_options(0x20000)  # SSL_OP_NO_COMPRESSION

        if self.insecure is not True:
            self.context.set_verify(OpenSSL.SSL.VERIFY_PEER,
                                    self.verify_callback)
        else:
            self.context.set_verify(OpenSSL.SSL.VERIFY_NONE,
                                    self.verify_callback)

        if self.cert_file:
            try:
                self.context.use_certificate_file(self.cert_file)
            except Exception, e:
                msg = 'Unable to load cert from "%s" %s' % (self.cert_file, e)
                raise exc.SSLConfigurationError(msg)
            if self.key_file is None:
                # We support having key and cert in same file
                try:
                    self.context.use_privatekey_file(self.cert_file)
                except Exception, e:
                    msg = ('No key file specified and unable to load key '
                           'from "%s" %s' % (self.cert_file, e))
                    raise exc.SSLConfigurationError(msg)

        if self.key_file:
            try:
                self.context.use_privatekey_file(self.key_file)
            except Exception, e:
                msg = 'Unable to load key from "%s" %s' % (self.key_file, e)
                raise exc.SSLConfigurationError(msg)

        if self.ca_file:
            try:
                self.context.load_verify_locations(self.ca_file)
            except Exception, e:
                msg = 'Unable to load CA from "%s"' % (self.ca_file, e)
                raise exc.SSLConfigurationError(msg)
        else:
            self.context.set_default_verify_paths()

    def connect(self):
        """
        Connect to an SSL port using the OpenSSL library and apply
        per-connection parameters.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self.timeout is not None:
            # '0' microseconds
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO,
                            struct.pack('LL', self.timeout, 0))
        self.sock = OpenSSLConnectionDelegator(self.context, sock)
        self.sock.connect((self.host, self.port))


class ResponseBodyIterator(object):
    """A class that acts as an iterator over an HTTP response."""

    def __init__(self, resp):
        self.resp = resp

    def __iter__(self):
        while True:
            yield self.next()

    def next(self):
        chunk = self.resp.read(CHUNKSIZE)
        if chunk:
            return chunk
        else:
            raise StopIteration()
