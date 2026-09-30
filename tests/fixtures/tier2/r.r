square <- function(x) {
  x * x
}

circle_area = function(r) {
  pi * square(r)
}

describe <- function(r) {
  inner <- function(v) format(v)
  paste("area", inner(circle_area(r)))
}

print(describe(2))
