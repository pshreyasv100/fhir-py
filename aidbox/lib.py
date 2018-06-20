import json
import copy

import requests
import inflection

from urllib.parse import parse_qsl, urlencode

from .utils import convert_to_underscore, convert_to_camelcase, convert_values
from .exceptions import (
    AidboxResourceFieldDoesNotExist, AidboxResourceNotFound,
    AidboxAuthorizationError, AidboxOperationOutcome)


class Aidbox:
    schema = None

    def __init__(self, host, token=None, email=None, password=None):
        self.schema = {}
        self.host = host

        if token:
            self.token = token
        else:
            r = requests.post(
                '{0}/oauth2/authorize'.format(host),
                params={
                    'client_id': 'sansara',
                    'scope': 'openid profile email',
                    'response_type': 'id_token',
                },
                data={'email': email, 'password': password},
                allow_redirects=False
            )
            if 'location' not in r.headers:
                raise AidboxAuthorizationError()

            token_data = dict(parse_qsl(r.headers['location']))
            self.token = token_data['id_token']

    def reference(self, resource_type, id, **kwargs):
        return AidboxReference(self, resource_type, id, **kwargs)

    def resource(self, resource_type, **kwargs):
        kwargs['resource_type'] = resource_type
        return AidboxResource(self, **kwargs)

    def resources(self, resource_type):
        return AidboxSearchSet(self, resource_type=resource_type)

    def _do_request(self, method, path, data=None, params=None):
        r = requests.request(
            method,
            '{0}/{1}'.format(self.host, path),
            params=params,
            json=convert_to_camelcase(data),
            headers={'Authorization': 'Bearer {0}'.format(self.token)})

        if 200 <= r.status_code < 300:
            result = json.loads(r.text) if r.text else None
            return convert_to_underscore(result)

        if r.status_code == 404:
            raise AidboxResourceNotFound()

        if r.status_code == 403:
            raise AidboxAuthorizationError()

        raise AidboxOperationOutcome(r.text)

    def _fetch_resource(self, path, params=None):
        return self._do_request('get', path, params=params)

    def _fetch_schema(self, resource_type):
        schema = self.schema.get(resource_type, None)
        if not schema:
            bundle = self._fetch_resource(
                'Attribute',
                params={'entity': resource_type}
            )
            attrs = [res['resource'] for res in bundle['entry']]
            schema = {inflection.underscore(attr['path'][0])
                      for attr in attrs} | {'id'}
            self.schema[resource_type] = schema

        return schema

    def __str__(self):
        return self.host

    def __repr__(self):
        return self.__str__()


class AidboxSearchSet:
    aidbox = None
    resource_type = None
    params = {}

    def __init__(self, aidbox, resource_type, params=None):
        self.aidbox = aidbox
        self.resource_type = resource_type
        self.params = params if params else {}

    def get(self, id):
        res = self.search(_id=id).first()
        if res:
            return res

        raise AidboxResourceNotFound()

    def execute(self):
        res_data = self.aidbox._fetch_resource(self.resource_type, self.params)
        resource_data = [res['resource'] for res in res_data['entry']]
        return [
            AidboxResource(
                self.aidbox,
                skip_validation=True,
                **data
            )
            for data in resource_data
            if data.get('resource_type') == self.resource_type
        ]

    def count(self):
        new_params = copy.deepcopy(self.params)
        new_params['_count'] = 1
        new_params['_totalMethod'] = 'count'

        # TODO: rewrite
        return self.aidbox._fetch_resource(
            self.resource_type,
            params=new_params
        )['total']

    def first(self):
        result = self.limit(1).execute()
        return result[0] if result else None

    def last(self):
        # TODO: return last item from list
        # TODO: sort (-) + first
        pass

    def clone(self, **kwargs):
        new_params = copy.deepcopy(self.params)
        new_params.update(kwargs)
        return AidboxSearchSet(self.aidbox, self.resource_type, new_params)

    def search(self, **kwargs):
        return self.clone(**kwargs)

    def limit(self, limit):
        return self.clone(_count=limit)

    def page(self, page):
        return self.clone(_page=page)

    def sort(self, keys):
        sort_keys = ','.join(keys) if isinstance(keys, list) else keys
        return self.clone(_sort=sort_keys)

    def include(self):
        # https://www.hl7.org/fhir/search.html
        # works as select_related
        # result: Bundle [patient1, patientN, clinic1, clinicN]
        # searchset.filter(name='john').get(pk=1)
        pass

    def revinclude(self):
        # https://www.hl7.org/fhir/search.html
        # works as prefetch_related
        pass

    def __str__(self):
        return '<AidboxSearchSet {0}?{1}>'.format(
            self.resource_type, urlencode(self.params))

    def __repr__(self):
        return self.__str__()

    def __iter__(self):
        return iter(self.execute())


class AidboxResource:
    aidbox = None
    resource_type = None
    _data = None
    _meta = None

    @property
    def root_attrs(self):
        return self.aidbox.schema[self.resource_type]

    def __init__(self, aidbox, skip_validation=False, **kwargs):
        self.aidbox = aidbox
        self.resource_type = kwargs.get('resource_type')
        self.aidbox._fetch_schema(self.resource_type)

        meta = kwargs.pop('meta', {})
        self._meta = meta
        self._data = {}

        for key, value in kwargs.items():
            try:
                setattr(self, key, value)
            except AidboxResourceFieldDoesNotExist:
                if not skip_validation:
                    raise

    def __setattr__(self, key, value):
        if key in dir(self):
            super(AidboxResource, self).__setattr__(key, value)
        elif key in self.root_attrs:
            self._data[key] = value
        else:
            raise AidboxResourceFieldDoesNotExist(
                'Invalid attribute `{0}` for resource `{1}`'.format(
                    key, self.resource_type))

    def __getattr__(self, key):
        if key in self.root_attrs:
            return self._data.get(key, None)
        else:
            raise AidboxResourceFieldDoesNotExist(
                'Invalid attribute `{0}` for resource `{1}`'.format(
                    key, self.resource_type))

    def get_path(self):
        if self.id:
            return '{0}/{1}'.format(self.resource_type, self.id)

        return self.resource_type

    def save(self):
        data = self.aidbox._do_request(
            'put' if self.id else 'post', self.get_path(), data=self.to_dict())

        self.meta = data.get('meta', {})
        self.id = data.get('id')

    def delete(self):
        return self.aidbox._do_request('delete', self.get_path())

    def reference(self, **kwargs):
        return AidboxReference(
            self.aidbox, self.resource_type, self.id, **kwargs)

    def to_dict(self):
        def convert_fn(item):
            if isinstance(item, AidboxResource):
                return item.reference().to_dict()
            elif isinstance(item, AidboxReference):
                return item.to_dict()
            else:
                return item

        return convert_values(
            self._data,
            convert_fn)

    def __str__(self):
        return '<AidboxResource {0}>'.format(self.get_path())

    def __repr__(self):
        return self.__str__()


class AidboxReference:
    aidbox = None
    resource_type = None
    id = None
    display = None
    resource = None

    def __init__(self, aidbox, resource_type, id, **kwargs):
        self.aidbox = aidbox
        self.resource_type = resource_type
        self.id = id
        self.display = kwargs.get('display', None)
        self.resource = kwargs.get('resource', None)

    def __str__(self):
        return '<AidboxReference {0}/{1}>'.format(self.resource_type, self.id)

    def __repr__(self):
        return self.__str__()

    def to_dict(self):
        return {attr: getattr(self, attr) for attr in [
            'id', 'resource_type', 'display', 'resource'
        ] if getattr(self, attr, None)}
