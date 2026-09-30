import gleam/float
import gleam/io

pub type Shape {
  Circle(radius: Float)
  Square(side: Float)
}

fn square(x: Float) -> Float {
  x *. x
}

pub fn area(shape: Shape) -> Float {
  case shape {
    Circle(r) -> 3.14159 *. square(r)
    Square(s) -> square(s)
  }
}

pub fn main() {
  io.println(float.to_string(area(Circle(2.0))))
}
