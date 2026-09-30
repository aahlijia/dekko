"""Rules for shapes."""

def _square(x):
    return x * x

def area(radius):
    return 3 * _square(radius)

def shape_library(name, radius):
    native.genrule(
        name = name,
        outs = [name + ".txt"],
        cmd = "echo %d > $@" % area(radius),
    )

DEFAULT_AREA = area(2)
