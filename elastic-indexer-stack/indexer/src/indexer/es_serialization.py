"""Use the same compact vector encoding for all Elasticsearch write routes."""
from elasticsearch.serializer import JsonSerializer,NdjsonSerializer

from .vector_json import dumps


class VectorJsonSerializer(JsonSerializer):
    def json_dumps(self,data):
        return dumps(data,default=self.default)


class VectorNdjsonSerializer(NdjsonSerializer):
    def json_dumps(self,data):
        return dumps(data,default=self.default)


def serializers():
    return {'application/json':VectorJsonSerializer(),
            'application/vnd.elasticsearch+json':VectorJsonSerializer(),
            'application/x-ndjson':VectorNdjsonSerializer(),
            'application/vnd.elasticsearch+x-ndjson':VectorNdjsonSerializer()}
