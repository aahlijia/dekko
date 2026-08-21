#ifndef PLAIN_H
#define PLAIN_H

// A genuine C header sitting in the same directory as widget.h -- the
// case a directory/sibling-count heuristic (rejected option 2 in the
// implementation plan) would get wrong, but per-file content-sniffing
// classifies correctly regardless of what else lives alongside it.

struct Point {
  int x;
  int y;
};

static int point_sum(struct Point p) {
  return p.x + p.y;
}

#endif
