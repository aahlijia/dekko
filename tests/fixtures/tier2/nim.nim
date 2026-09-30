type
  Shape* = ref object of RootObj
  Circle* = ref object of Shape
    radius: float

proc square(x: float): float =
  x * x

method area*(c: Circle): float =
  3.14159 * square(c.radius)

proc describe*(c: Circle): string =
  $area(c)

echo describe(Circle(radius: 2.0))
