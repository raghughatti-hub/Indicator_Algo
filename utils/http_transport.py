"""Give legacy broker SDK methods bounded HTTP calls without global monkeypatches."""
import inspect
from types import FunctionType, MethodType


class BoundedRequests:
    def __init__(self, module):
        self.module = module

    def __getattr__(self, name):
        value = getattr(self.module, name)
        if name not in {'request', 'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}:
            return value
        def bounded(*args, **kwargs):
            if kwargs.get('timeout') is None:
                kwargs['timeout'] = (3.05, 10)
            return value(*args, **kwargs)
        return bounded


def bound_sdk_http(api):
    for name in dir(api):
        method = getattr(api, name)
        if not inspect.ismethod(method):
            continue
        original = method.__func__
        module = original.__globals__.get('requests')
        if module is None:
            continue
        context = dict(original.__globals__, requests=BoundedRequests(module))
        wrapped = FunctionType(original.__code__, context, original.__name__, original.__defaults__, original.__closure__)
        wrapped.__kwdefaults__ = original.__kwdefaults__
        setattr(api, name, MethodType(wrapped, api))
    return api
