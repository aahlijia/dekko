#include <cmath>

namespace geo {

class Circle {
  public:
    Circle(double r);
    double area() const;

  private:
    double r_;
};

double pi() {
    return 3.14159;
}

Circle::Circle(double r) : r_(r) {}

double Circle::area() const {
    return pi() * r_ * r_;
}

}  // namespace geo
