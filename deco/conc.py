import multiprocessing
import multiprocessing.reduction
from multiprocessing import Process, Pipe, Queue, Pool
from threading import Thread
from itertools import izip
import time, inspect, ast
import marshal, types
from ast import NodeTransformer

def concWrapper(args, global_args):
        globals().update(global_args)
        result = f(*args)
        operations = [inner for outer in args if type(outer) is argProxy for inner in outer.operations]
        return result, operations

class argProxy(object):
    def __init__(self, arg_id, value):
        self.arg_id = arg_id
        self.operations = []
        self.value = value

    def __getattr__(self, name):
        if hasattr(self, 'value') and hasattr(self.value, name):
            return getattr(self.value, name)
        raise AttributeError

    def __setitem__(self, key, value):
        self.value.__setitem__(key, value)
        self.operations.append((self.arg_id, key, value))

    def __getitem__(self, key):
        return self.value.__getitem__(key)

class SchedulerRewriter(NodeTransformer):
    def __init__(self):
        self.arguments = []
        self.encountered_funcs = set()
    def generic_visit(self, node):
        super(NodeTransformer, self).generic_visit(node)
        if hasattr(node, 'body'):
            returns = [i for i, child in enumerate(node.body) if type(child) is ast.Return]
            if len(returns) > 0:
                for wait in self.get_waits():
                    node.body.insert(returns[0], wait)
            for i, child in enumerate(node.body):
                if type(child) is ast.Assign and type(child.value) is ast.Call:
                    call = child.value
                    self.encountered_funcs.add(call.func.id)
                    name = child.targets[0].value
                    index = child.targets[0].slice.value
                    call.func = ast.Attribute(call.func, 'assign', ast.Load())
                    call.args = [ast.Tuple([name, index], ast.Load())] + call.args
                    node.body[i] = ast.Expr(call)
    def get_waits(self):
        return [ast.Expr(ast.Call(ast.Attribute(ast.Name(fname, ast.Load()), 'wait', ast.Load()), [], [], None, None)) for fname in self.encountered_funcs]
    def visit_FunctionDef(self, node):
        node.decorator_list = []
        self.generic_visit(node)
        node.body += self.get_waits()
        return node

class synchronized(object):
    def __init__(self, f):
        source = inspect.getsourcelines(f)
        source = "".join(source[0])
        fast = ast.parse(source)
        node = fast
        rewriter = SchedulerRewriter()
        rewriter.visit(node.body[0])
        ast.fix_missing_locations(node)
        out = compile(node, "<string>", "exec")
        exec out in f.func_globals
        self.f = f.func_globals['test_size']

    def __call__(self, *args, **kwargs):
        return self.f(*args, **kwargs)

class concurrent(object):
    params = ['processes']
    functions = []
    def __init__(self, *args, **kwargs):
        self.processes = 3
        if len(args) > 0 and type(args[0]) == types.FunctionType:
            self.setFunction(args[0])
        else:
            self.__dict__.update({concurrent.params[i]: arg for i, arg in enumerate(args)})
            self.__dict__.update({key: kwargs[key] for key in concurrent.params if key in kwargs})
        self.results = []
        self.assigns = []
        self.arg_proxies = {}
        self.p = None

    def replaceWithProxies(self, args):
        for i, arg in enumerate(args):
            if type(arg) is dict or type(arg) is list:
                if not id(arg) in self.arg_proxies:
                    self.arg_proxies[id(arg)] = argProxy(id(arg), arg)
                args[i] = self.arg_proxies[id(arg)]

    def setFunction(self, f):
        concurrent.functions.append(f.__name__)
        def findFreeNames(f):
            source = inspect.getsourcelines(f)
            source = "".join(source[0])
            fast = ast.parse(source)
            f_args_names = set([a.id for a in fast.body[0].args.args])
            f_body = fast.body[0].body
            f_vars_names = set()
            f_free_names = set()
            for line in f_body:
                for n in ast.walk(line):
                    if isinstance(n, ast.Name):
                        f_vars_names.add(n.id)
            f_free_names = f_vars_names.difference(f_args_names)
            return f_free_names
        self.f = f
        globals()['f'] = f
        self.free_names = findFreeNames(f)
    def assign(self, target, *args):
        self.assigns.append((target, self(*args)))
    def __call__(self, *args):
        if len(args) > 0 and type(args[0]) == types.FunctionType:
            self.setFunction(args[0])
            return self
        if self.p == None:
            self.p = Pool(self.processes)
        args = list(args)
        frm = inspect.stack()[1]
        mod = inspect.getmodule(frm[0])
        global_arg_keys = [g for g in self.free_names if hasattr(mod, g) and type(getattr(mod, g)) != types.ModuleType]
        global_args = [getattr(mod, g) for g in global_arg_keys]
        self.replaceWithProxies(args)
        self.replaceWithProxies(global_args)
        result = self.p.apply_async(concWrapper, [args, dict(zip(global_arg_keys, global_args))])
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
