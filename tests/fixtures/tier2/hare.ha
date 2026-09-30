use fmt;

export type circle = struct {
	radius: f64,
};

fn square(x: f64) f64 = {
	return x * x;
};

export fn area(c: *circle) f64 = {
	return 3.14159 * square(c.radius);
};

export fn main() void = {
	const c = circle { radius = 2.0 };
	fmt::println(area(&c))!;
};
