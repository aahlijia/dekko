class Svc:
    x: Foo
    k = 3

    def __init__(self):
        self.a = Bar()
        self.b = helper()
        self.c: Baz = None
        self.d = []
        e = Foo()

    def other(self):
        self.late = mod.Thing()
