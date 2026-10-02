abstract class Shape {
  double area();
}

class Circle extends Shape {
  Circle(this.radius);

  final double radius;

  double get diameter => radius * 2;

  @override
  double area() {
    return 3.14159 * square(radius);
  }
}

double square(double x) => x * x;

void main() {
  print(Circle(2).area());
}
