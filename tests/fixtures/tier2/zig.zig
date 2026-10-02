const std = @import("std");

pub const Circle = struct {
    radius: f64,

    pub fn area(self: Circle) f64 {
        return std.math.pi * square(self.radius);
    }
};

fn square(x: f64) f64 {
    return x * x;
}

pub fn main() void {
    const c = Circle{ .radius = 2 };
    std.debug.print("{d}\n", .{c.area()});
}
