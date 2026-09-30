package shapes

import "core:fmt"

Circle :: struct {
	radius: f64,
}

Kind :: enum {
	Round,
	Flat,
}

square :: proc(x: f64) -> f64 {
	return x * x
}

area :: proc(c: Circle) -> f64 {
	return 3.14159 * square(c.radius)
}

main :: proc() {
	fmt.println(area(Circle{radius = 2}))
}
