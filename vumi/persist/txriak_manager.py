# -*- test-case-name: vumi.persist.tests.test_txriak_manager -*-

"""An async manager implementation on top of the riak Python package."""

from riak import RiakObject, RiakMapReduce, RiakError
from twisted.internet.threads import deferToThread
from twisted.internet.defer import (
    inlineCallbacks, returnValue, gatherResults, maybeDeferred, succeed)

from vumi.persist.model import Manager, VumiRiakError
from vumi.persist.riak_base import (
    VumiRiakClientBase, VumiIndexPageBase, VumiRiakBucketBase,
    VumiRiakObjectBase)


def riakErrorHandler(failure):
    e = failure.trap(RiakError)
    raise VumiRiakError(e)


class VumiTxRiakClient(VumiRiakClientBase):
    """
    Wrapper around a RiakClient to manage resources better.
    """


class VumiTxIndexPage(VumiIndexPageBase):
    """
    Wrapper around a page of index query results.

    Iterating over this object will return the results for the current page.
    """

    # Methods that touch the network.

    def next_page(self):
        """
        Fetch the next page of results.

        :returns:
            A new :class:`VumiTxIndexPage` object containing the next page of
            results.
        """
        if not self.has_next_page():
            return succeed(None)
        d = deferToThread(self._index_page.next_page)
        d.addCallback(type(self))
        d.addErrback(riakErrorHandler)
        return d


class VumiTxRiakBucket(VumiRiakBucketBase):
    """
    Wrapper around a RiakBucket to manage network access better.
    """

    # Methods that touch the network.

    def get_index(self, index_name, start_value, end_value=None,
                  return_terms=None):
        d = self.get_index_page(
            index_name, start_value, end_value, return_terms=return_terms)
        d.addCallback(list)
        return d

    def get_index_page(self, index_name, start_value, end_value=None,
                       return_terms=None, max_results=None, continuation=None):
        d = deferToThread(
            self._riak_bucket.get_index, index_name, start_value, end_value,
            return_terms=return_terms, max_results=max_results,
            continuation=continuation)
        d.addCallback(VumiTxIndexPage)
        d.addErrback(riakErrorHandler)
        return d


class VumiTxRiakObject(VumiRiakObjectBase):
    """
    Wrapper around a RiakObject to manage network access better.
    """

    def get_bucket(self):
        return VumiTxRiakBucket(self._riak_obj.bucket)

    # Methods that touch the network.

    def _call_and_wrap(self, func):
        """
        Call a function that touches the network and wrap the result in this
        class.
        """
        d = deferToThread(func)
        d.addCallback(type(self))
        return d


class TxRiakManager(Manager):
    """An async persistence manager for the riak Python package."""

    call_decorator = staticmethod(inlineCallbacks)

    @classmethod
    def from_config(cls, config):
        config = config.copy()
        bucket_prefix = config.pop('bucket_prefix')
        load_bunch_size = config.pop(
            'load_bunch_size', cls.DEFAULT_LOAD_BUNCH_SIZE)
        mapreduce_timeout = config.pop(
            'mapreduce_timeout', cls.DEFAULT_MAPREDUCE_TIMEOUT)
        transport_type = config.pop('transport_type', 'http')
        store_versions = config.pop('store_versions', None)

        host = config.get('host', '127.0.0.1')
        port = config.get('port')
        prefix = config.get('prefix', 'riak')
        mapred_prefix = config.get('mapred_prefix', 'mapred')
        client_id = config.get('client_id')
        transport_options = config.get('transport_options', {})

        client_args = dict(
            host=host, prefix=prefix, mapred_prefix=mapred_prefix,
            protocol=transport_type, client_id=client_id,
            transport_options=transport_options)

        if port is not None:
            client_args['port'] = port

        client = VumiTxRiakClient(**client_args)
        return cls(
            client, bucket_prefix, load_bunch_size=load_bunch_size,
            mapreduce_timeout=mapreduce_timeout, store_versions=store_versions)

    def close_manager(self):
        return deferToThread(self.client.close)

    def riak_bucket(self, bucket_name):
        bucket = self.client.bucket(bucket_name)
        if bucket is not None:
            bucket = VumiTxRiakBucket(bucket)
        return bucket

    def riak_object(self, modelcls, key, result=None):
        bucket = self.bucket_for_modelcls(modelcls)._riak_bucket
        riak_object = VumiTxRiakObject(RiakObject(self.client, bucket, key))
        if result:
            metadata = result['metadata']
            indexes = metadata['index']
            if hasattr(indexes, 'items'):
                # TODO: I think this is a Riak bug. In some cases
                #       (maybe when there are no indexes?) the index
                #       comes back as a list, in others (maybe when
                #       there are indexes?) it comes back as a dict.
                indexes = indexes.items()
            data = result['data']
            riak_object.set_content_type(metadata['content-type'])
            riak_object.set_indexes(indexes)
            riak_object.set_encoded_data(data)
        else:
            riak_object.set_content_type("application/json")
            riak_object.set_data({'$VERSION': modelcls.VERSION})
        return riak_object

    def store(self, modelobj):
        riak_object = modelobj._riak_object
        modelcls = type(modelobj)
        model_name = "%s.%s" % (modelcls.__module__, modelcls.__name__)
        store_version = self.store_versions.get(model_name, modelcls.VERSION)
        # Run reverse migrators until we have the correct version of the data.
        data_version = riak_object.get_data().get('$VERSION', None)
        while data_version != store_version:
            migrator = modelcls.MIGRATOR(
                modelcls, self, data_version, reverse=True)
            riak_object = migrator(riak_object).get_riak_object()
            data_version = riak_object.get_data().get('$VERSION', None)
        d = riak_object.store()
        d.addCallback(lambda _: modelobj)
        return d

    def delete(self, modelobj):
        d = modelobj._riak_object.delete()
        d.addCallback(lambda _: None)
        return d

    @inlineCallbacks
    def load(self, modelcls, key, result=None):
        riak_object = self.riak_object(modelcls, key, result)
        if not result:
            yield riak_object.reload()
        was_migrated = False

        # Run migrators until we have the correct version of the data.
        while riak_object.get_data() is not None:
            data_version = riak_object.get_data().get('$VERSION', None)
            if data_version == modelcls.VERSION:
                obj = modelcls(self, key, _riak_object=riak_object)
                obj.was_migrated = was_migrated
                returnValue(obj)
            migrator = modelcls.MIGRATOR(modelcls, self, data_version)
            riak_object = migrator(riak_object).get_riak_object()
            was_migrated = True
        returnValue(None)

    def _load_multiple(self, modelcls, keys):
        d = gatherResults([self.load(modelcls, key) for key in keys])
        d.addCallback(lambda objs: [obj for obj in objs if obj is not None])
        return d

    def riak_map_reduce(self):
        mapreduce = RiakMapReduce(self.client)
        # Hack: We replace the two methods that hit the network with
        #       deferToThread wrappers to prevent accidental sync calls in
        #       other code.
        run = mapreduce.run
        stream = mapreduce.stream
        mapreduce.run = lambda *a, **kw: deferToThread(run, *a, **kw)
        mapreduce.stream = lambda *a, **kw: deferToThread(stream, *a, **kw)
        return mapreduce

    def run_map_reduce(self, mapreduce, mapper_func=None, reducer_func=None):
        def map_results(raw_results):
            deferreds = []
            for row in raw_results:
                deferreds.append(maybeDeferred(mapper_func, self, row))
            return gatherResults(deferreds)

        mapreduce_done = mapreduce.run(timeout=self.mapreduce_timeout)
        if mapper_func is not None:
            mapreduce_done.addCallback(map_results)
        if reducer_func is not None:
            mapreduce_done.addCallback(lambda r: reducer_func(self, r))
        return mapreduce_done

    def _search_iteration(self, bucket, query, rows, start):
        d = deferToThread(bucket.search, query, rows=rows, start=start)
        d.addCallback(lambda r: [doc["id"] for doc in r["docs"]])
        return d

    @inlineCallbacks
    def real_search(self, modelcls, query, rows=None, start=None):
        rows = 1000 if rows is None else rows
        bucket_name = self.bucket_name(modelcls)
        bucket = self.client.bucket(bucket_name)
        if start is not None:
            keys = yield self._search_iteration(bucket, query, rows, start)
            returnValue(keys)
        keys = []
        new_keys = yield self._search_iteration(bucket, query, rows, 0)
        while new_keys:
            keys.extend(new_keys)
            new_keys = yield self._search_iteration(
                bucket, query, rows, len(keys))
        returnValue(keys)

    def riak_enable_search(self, modelcls):
        bucket_name = self.bucket_name(modelcls)
        bucket = self.client.bucket(bucket_name)
        return deferToThread(bucket.enable_search)

    def riak_search_enabled(self, modelcls):
        bucket_name = self.bucket_name(modelcls)
        bucket = self.client.bucket(bucket_name)
        return deferToThread(bucket.search_enabled)

    def should_quote_index_values(self):
        return False

    def purge_all(self):
        return deferToThread(self.client._purge_all, self.bucket_prefix)
