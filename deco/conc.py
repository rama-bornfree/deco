from multiprocessing import Pool
import inspect
import ast
from . import astutil
import types


def concWrapper(f, args, kwargs):
    result = concurrent.functions[f](*args, **kwargs)
    operations = [inner for outer in args if type(outer) is argProxy for inner in outer.operations]
    return result, operations


class argProxy(object):
    def __init__(self, arg_id, value):
        self.arg_id = arg_id
        self.operations = []
        self.value = value

    def __getattr__(self, name):
        if name in ["__getstate__", "__setstate__"]:
            raise AttributeError
        if hasattr(self, 'value') and hasattr(self.value, name):
            return getattr(self.value, name)
        raise AttributeError

    def __setitem__(self, key, value):
        self.value.__setitem__(key, value)
        self.operations.append((self.arg_id, key, value))

    def __getitem__(self, key):
        return self.value.__getitem__(key)


class synchronized(object):
    def __init__(self, f):
        self.orig_f = f
        self.f = None
        self.ast = None

    def __call__(self, *args, **kwargs):
        if self.f is None:
            source = inspect.getsourcelines(self.orig_f)
            source = "".join(source[0])
            self.ast = ast.parse(source)
            rewriter = astutil.SchedulerRewriter(concurrent.functions.keys())
            rewriter.visit(self.ast.body[0])
            ast.fix_missing_locations(self.ast)
            out = compile(self.ast, "<string>", "exec")
            scope = dict(self.orig_f.__globals__)
            exec(out, scope)
            self.f = scope[self.orig_f.__name__]
        return self.f(*args, **kwargs)


class concurrent(object):
    functions = {}

    @staticmethod
    def custom(constructor = None, apply_async = None):
        def _custom_concurrent(*args, **kwargs):
            conc = concurrent(*args, **kwargs)
            if constructor is not None: conc.conc_constructor = constructor
            if apply_async is not None: conc.apply_async = apply_async
            return conc
        return _custom_concurrent

    def __init__(self, *args, **kwargs):
        self.conc_args = []
        self.conc_kwargs = {}
        if len(args) > 0 and isinstance(args[0], types.FunctionType):
            self.setFunction(args[0])
        else:
            self.conc_args = args
            self.conc_kwargs = kwargs
        self.results = []
        self.assigns = []
        self.arg_proxies = {}
        self.conc_constructor = Pool
        self.apply_async = lambda self, function, args: self.concurrency.apply_async(function, args)
        self.concurrency = None

    def replaceWithProxies(self, args):
        args_iter = args.iteritems() if type(args) is dict else enumerate(args)
        for i, arg in args_iter:
            if type(arg) is dict or type(arg) is list:
                if not id(arg) in self.arg_proxies:
                    self.arg_proxies[id(arg)] = argProxy(id(arg), arg)
                args[i] = self.arg_proxies[id(arg)]

    def setFunction(self, f):
        concurrent.functions[f.__name__] = f
        self.f_name = f.__name__

    def assign(self, target, *args, **kwargs):
        self.assigns.append((target, self(*args, **kwargs)))

    def __call__(self, *args, **kwargs):
        if len(args) > 0 and isinstance(args[0], types.FunctionType):
            self.setFunction(args[0])
            return self
        if self.concurrency is None:
            self.concurrency = self.conc_constructor(*self.conc_args, **self.conc_kwargs)
        args = list(args)
        self.replaceWithProxies(args)
        self.replaceWithProxies(kwargs)
        result = self.apply_async(self, concWrapper, [self.f_name, args, kwargs])
        self.results.append(result)
        return result

    def process_operation_queue(self, ops):
        for arg_id, key, value in ops:
            self.arg_proxies[arg_id].value.__setitem__(key, value)

    def wait(self):
        results = []
        while len(self.results) > 0:
            result, operations = self.results.pop().get()
            self.process_operation_queue(operations)
            results.append(result)
        for assign in self.assigns:
            assign[0][0][assign[0][1]] = assign[1].get()[0]
        self.arg_proxies = {}
        return results
