import util
from util import helper


def run(args: list[str], *extra, **kw) -> int:
    cfg = util.Config()
    cfg.validate()
    return helper(len(args))


run([])
